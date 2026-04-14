"""
Scalper — Short-Term Momentum/Mean-Reversion Trading

Strategy:
1. Every tick, fetch markets with recent price changes from Gamma API
2. Mean reversion: if price dropped >2% in 1 hour, buy (expect bounce)
3. Momentum: if price moved >3% in 1 day with high volume, ride it
4. Exit: take profit at +2-3%, stop loss at -3%, or max hold time

Targets markets resolving within 7 days (active, not dead long-term bets).
"""
import time
import logging
import requests
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from market_fetcher import Market
from executor import Executor
from risk_manager import RiskManager
import config
import db

logger = logging.getLogger(__name__)


class Scalper:
    def __init__(self, fetcher, executor: Executor, risk_mgr: RiskManager):
        self.fetcher = fetcher
        self.executor = executor
        self.risk_mgr = risk_mgr
        self._last_fetch = 0
        self._markets_cache = []
        self._price_history: dict[str, list[tuple[float, float]]] = {}  # market_id -> [(timestamp, price)]
        self._HISTORY_WINDOW = 300  # Keep 5 minutes of history
        self._pending_orders: dict[str, dict] = {}  # token_id -> order info
        self._failed_exits: set[str] = set()  # token_ids where exit failed — don't retry
        self._ws_price_changes: list[dict] = []  # Real-time price changes from WebSocket

    def on_price_change(self, token_id: str, new_price: float, old_price: float, change: float):
        """Called by MarketFeed WebSocket when a price changes in real-time."""
        self._ws_price_changes.append({
            "token_id": token_id,
            "new_price": new_price,
            "old_price": old_price,
            "change": change,
            "timestamp": time.time(),
        })
        # Keep only last 100 changes
        if len(self._ws_price_changes) > 100:
            self._ws_price_changes = self._ws_price_changes[-100:]

    def tick(self) -> list[dict]:
        """Run one scalp cycle. Called every SCALP_SCAN_INTERVAL seconds."""
        actions = []

        if not config.TRADING_ENABLED:
            return actions

        # 1. CHECK EXITS FIRST — close positions hitting TP/SL/time
        exit_actions = self._check_exits()
        actions.extend(exit_actions)

        # 2. Fetch markets and track prices every tick
        now = time.time()
        if now - self._last_fetch >= config.SCALP_SCAN_INTERVAL:
            self._markets_cache = self._fetch_movers()
            self._last_fetch = now

        if not self._markets_cache:
            return actions

        # Get current positions to avoid duplicates
        existing = []
        try:
            existing = db.get_live_positions() if config.DATABASE_URL else []
        except Exception:
            pass
        # Ignore dust positions (< $1 value)
        real_positions = [p for p in existing if (p.get("current_value", 0) or p.get("total_cost", 0) or 0) >= 1]
        existing_questions = {p.get("question", "").lower().strip() for p in real_positions}
        open_count = len(real_positions)
        if open_count >= config.SCALP_MAX_CONCURRENT:
            return actions

        # Find entry signals
        for mkt in self._markets_cache:
            if open_count >= config.SCALP_MAX_CONCURRENT:
                break

            question = mkt.get("question", "")
            q_key = question.lower().strip()

            # Skip if we have ANY position on this market (checked from DB every tick)
            if q_key in existing_questions:
                continue
            # Skip if we already attempted this market recently (prevents retry spam)
            attempted = {v.get("question", "").lower().strip() for v in self._pending_orders.values()
                         if time.time() - v.get("attempted_at", 0) < 300}  # 5 min cooldown
            if q_key in attempted:
                continue

            signal = self._check_signal(mkt)
            if signal:
                action = self._enter(mkt, signal)
                if action:
                    actions.append(action)
                    open_count += 1
                    existing_questions.add(question.lower().strip())

        if actions:
            logger.info(f"Scalper opened {len(actions)} trades")

        return actions

    def _check_exits(self) -> list[dict]:
        """Check all open positions for take-profit, stop-loss, or max hold time."""
        actions = []
        positions = []
        try:
            positions = db.get_live_positions() if config.DATABASE_URL else []
        except Exception:
            return actions

        for pos in positions:
            entry = pos.get("entry_price", 0) or 0
            cur = pos.get("cur_price", 0) or 0
            token_id = pos.get("token_id", "")
            shares = pos.get("num_shares", 0) or 0
            question = pos.get("question", "")

            if not entry or not cur or not token_id or shares <= 0:
                continue

            # Don't retry failed exits
            if token_id in self._failed_exits:
                continue

            change = (cur - entry) / entry if entry > 0 else 0

            # Log positions with significant P&L
            if abs(change) > 0.02:
                logger.info(f"  Position: {question[:35]} entry={entry:.3f} cur={cur:.3f} change={change:+.1%}")

            # TP sell is already placed on entry — don't touch it, let it fill
            # Only intervene for stop loss or max hold time
            reason = None

            # Stop loss — only if loss exceeds threshold
            if change <= -config.SCALP_STOP_LOSS:
                reason = f"SL hit: {change:+.1%} (entry={entry:.3f} now={cur:.3f})"

            # Max hold time
            elif pos.get("opened_at"):
                try:
                    opened = datetime.fromisoformat(str(pos["opened_at"]).replace("Z", "+00:00"))
                    held_minutes = (datetime.now(timezone.utc) - opened).total_seconds() / 60
                    if held_minutes >= config.SCALP_MAX_HOLD_MINUTES:
                        reason = f"Max hold {held_minutes:.0f}min"
                except Exception:
                    pass

            if reason:
                logger.info(f"SCALP EXIT: {question[:40]} — {reason}")
                try:
                    # Limit sell at current price to get out
                    sell_price = round(max(cur, 0.01), 2)
                    if int(shares) >= 5 and sell_price >= 0.01:
                        order = self.executor.place_order(
                            token_id=token_id,
                            side="SELL",
                            price=sell_price,
                            size=int(shares),
                            order_type="GTC",
                        )
                        if order:
                            actions.append({
                                "action": "exit",
                                "question": question,
                                "reason": reason,
                                "pnl": round((cur - entry) * shares, 2),
                            })
                        else:
                            self._failed_exits.add(token_id)
                except Exception as e:
                    logger.error(f"Exit order failed: {e}")
                    self._failed_exits.add(token_id)

        return actions

    def _fetch_movers(self) -> list[dict]:
        """Fetch markets with price movement — prioritizes live sports events."""
        try:
            all_raw = []

            # 1. Fetch newest events (sports games, crypto 5-min, etc)
            # Sorted by startDate descending = most recent/upcoming first
            try:
                resp = requests.get(
                    "https://gamma-api.polymarket.com/events",
                    params={
                        "active": "true", "closed": "false",
                        "limit": 200, "order": "volume24hr", "ascending": "false",
                    },
                    headers={"User-Agent": "Mozilla/5.0"},
                    proxies={"http": None, "https": None},
                    timeout=15,
                )
                if resp.status_code == 200:
                    events = resp.json()
                    for event in events:
                        for m in event.get("markets", []):
                            m["_event"] = event.get("title", "")
                            m["_event_slug"] = event.get("slug", "")
                            all_raw.append(m)
                    logger.info(f"Found {len(all_raw)} markets from {len(events)} recent events")

                # Subscribe to WebSocket price updates for these markets
                if hasattr(self, '_market_feed') and self._market_feed:
                    token_ids = []
                    for event in events:
                        for m in event.get("markets", []):
                            tids = m.get("clobTokenIds", "[]")
                            if isinstance(tids, str):
                                try: tids = json.loads(tids)
                                except: tids = []
                            token_ids.extend(tids)
                    if token_ids:
                        self._market_feed.subscribe(token_ids)
            except Exception as e:
                logger.warning(f"Events fetch error: {e}")

            # 2. Also fetch top regular markets by volume
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 50,
                    "order": "volume_24hr",
                    "ascending": "false",
                },
                headers={"User-Agent": "Mozilla/5.0"},
                proxies={"http": None, "https": None},
                timeout=15,
            )
            if resp.status_code == 200:
                all_raw.extend(resp.json())

            markets = all_raw
            now = datetime.now(timezone.utc)
            movers = []

            for m in markets:
                # Filter: must have price change data
                h_change = float(m.get("oneHourPriceChange") or 0)
                d_change = float(m.get("oneDayPriceChange") or 0)
                vol24 = float(m.get("volume24hr") or 0)
                liq = float(m.get("liquidity") or 0)

                # Must have liquidity (skip dead markets)
                # Don't filter on price change — live sports update too fast for hourly stats
                if liq < config.SCALP_MIN_LIQUIDITY:
                    continue

                # Parse prices
                prices = m.get("outcomePrices", "[]")
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                    except:
                        prices = []
                yes_price = float(prices[0]) if prices else 0

                # Skip extreme prices — don't buy longshots or near-certainties
                if yes_price < 0.20 or yes_price > 0.80:
                    continue

                # Parse token IDs
                token_ids = m.get("clobTokenIds", "[]")
                if isinstance(token_ids, str):
                    try:
                        token_ids = json.loads(token_ids)
                    except:
                        token_ids = []

                if not token_ids or len(token_ids) < 2:
                    continue

                # Check resolution time — ONLY trade markets ending within 48 hours
                end = m.get("endDateIso", "")
                hours_left = None
                if end:
                    try:
                        end_dt = datetime.fromisoformat(end + "T23:59:59+00:00") if len(end) == 10 else datetime.fromisoformat(end.replace("Z", "+00:00"))
                        hours_left = (end_dt - now).total_seconds() / 3600
                    except:
                        pass

                # ONLY trade markets ending within 48 hours in the FUTURE
                if hours_left is None or hours_left > 48 or hours_left < 0:
                    continue

                # Boost priority for confirmed live games
                is_live = False
                event_slug = m.get("_event_slug") or m.get("slug", "")
                if hasattr(self, '_sports_feed') and self._sports_feed:
                    for game in self._sports_feed.get_live_games():
                        if game.get("slug", "") and game["slug"] in event_slug:
                            is_live = True
                            break

                movers.append({
                    "question": m.get("question", ""),
                    "market_id": m.get("id", ""),
                    "condition_id": m.get("condition_id", ""),
                    "yes_price": yes_price,
                    "no_price": float(prices[1]) if len(prices) > 1 else 1 - yes_price,
                    "yes_token": token_ids[0],
                    "no_token": token_ids[1] if len(token_ids) > 1 else "",
                    "h_change": h_change,
                    "d_change": d_change,
                    "vol24": vol24,
                    "liq": liq,
                    "hours_left": hours_left,
                    "is_live": is_live,
                    "neg_risk": m.get("negRisk", False),
                    "tick_size": m.get("orderPriceMinTickSize", "0.01"),
                    "event_slug": event_slug,
                })

            # Use WebSocket prices if available — these update in real-time
            if hasattr(self, '_market_feed') and self._market_feed:
                for mkt in movers:
                    ws_price = self._market_feed.get_price(mkt["yes_token"])
                    if ws_price and ws_price > 0:
                        mkt["yes_price"] = ws_price
                    ws_no = self._market_feed.get_price(mkt["no_token"])
                    if ws_no and ws_no > 0:
                        mkt["no_price"] = ws_no

            # Track price history for change detection
            now_ts = time.time()
            for mkt in movers:
                mid = mkt["market_id"]
                price = mkt["yes_price"]
                if mid not in self._price_history:
                    self._price_history[mid] = []
                self._price_history[mid].append((now_ts, price))
                self._price_history[mid] = [
                    (t, p) for t, p in self._price_history[mid]
                    if now_ts - t < self._HISTORY_WINDOW
                ]
                history = self._price_history[mid]
                if len(history) >= 2:
                    prev_price = history[-2][1]
                    mkt["tick_change"] = (price - prev_price) / prev_price if prev_price > 0 else 0
                    oldest_price = history[0][1]
                    mkt["rt_change"] = (price - oldest_price) / oldest_price if oldest_price > 0 else 0

                    # Use real-time changes as h_change/d_change fallback when Gamma API has zeros
                    if mkt["h_change"] == 0 and abs(mkt["rt_change"]) > 0:
                        mkt["h_change"] = mkt["rt_change"]
                    if mkt["d_change"] == 0 and abs(mkt["rt_change"]) > 0:
                        mkt["d_change"] = mkt["rt_change"]
                else:
                    mkt["tick_change"] = 0
                    mkt["rt_change"] = 0

            # Sort by real-time change (most volatile), fall back to hourly
            movers.sort(key=lambda x: abs(x["h_change"]) + abs(x["d_change"]), reverse=True)
            with_signal = sum(1 for m in movers if abs(m["h_change"]) >= config.SCALP_MEAN_REVERSION_THRESHOLD or (abs(m["d_change"]) > 0.03 and m["vol24"] > 5000))
            logger.info(f"Scalper: {len(movers)} active markets, {with_signal} with signals")
            if movers and with_signal == 0:
                top = movers[0]
                logger.info(f"  Top market: {top['question'][:40]} h={top['h_change']:+.4f} d={top['d_change']:+.4f} vol={top['vol24']:.0f}")
            return movers

        except Exception as e:
            logger.error(f"Scalper fetch error: {e}")
            return []

    def _check_signal(self, mkt: dict) -> Optional[dict]:
        """Check if a market has a tradeable signal."""
        h = mkt["h_change"]
        d = mkt["d_change"]
        vol24 = mkt["vol24"]
        price = mkt["yes_price"]
        no_price = mkt["no_price"]
        threshold = config.SCALP_MEAN_REVERSION_THRESHOLD

        # Buy the side that dropped — expect mean reversion
        if h < -threshold:
            return {"type": "dip", "direction": "yes", "token_id": mkt["yes_token"],
                    "price": price, "reason": f"YES dropped {h:+.1%} in 1h"}

        if h > threshold:
            return {"type": "dip", "direction": "no", "token_id": mkt["no_token"],
                    "price": no_price, "reason": f"YES spiked {h:+.1%} in 1h, buying NO"}

        # Daily momentum with volume
        if d < -0.03 and vol24 > 5000:
            return {"type": "momentum", "direction": "yes", "token_id": mkt["yes_token"],
                    "price": price, "reason": f"YES down {d:+.1%} daily, vol ${vol24:,.0f}"}

        if d > 0.03 and vol24 > 5000:
            return {"type": "momentum", "direction": "no", "token_id": mkt["no_token"],
                    "price": no_price, "reason": f"YES up {d:+.1%} daily, buying NO, vol ${vol24:,.0f}"}

        return None

    def _enter(self, mkt: dict, signal: dict) -> Optional[dict]:
        """Place a buy order for a scalp trade."""
        price = signal["price"]
        if price <= 0:
            return None

        # Use cached balance, account for existing positions
        available = self.risk_mgr.current_bankroll
        positions = []
        try:
            positions = db.get_live_positions() if config.DATABASE_URL else []
        except Exception:
            pass
        deployed = sum(p.get("total_cost", 0) or p.get("current_value", 0) or 0 for p in positions)
        free_cash = max(0, available - deployed)

        spend = min(config.SCALP_MAX_POSITION_SIZE, free_cash * 0.3)  # Max 30% of free cash
        if spend < 1:
            return None

        logger.info(
            f"SCALP ENTRY: {signal['type']} {signal['direction'].upper()} "
            f"{mkt['question'][:40]} — spending ${spend:.2f} at market "
            f"({signal['reason']})"
        )

        # Limit buy at current price — maker order = 0% fee
        num_shares = int(spend / price) if price > 0 else 0
        if num_shares < 5:
            return None
        order = self.executor.place_order(
            token_id=signal["token_id"],
            side="BUY",
            price=round(price, 2),
            size=num_shares,
            order_type="GTC",
        )

        # Track attempt regardless of success to prevent retry spam
        self._pending_orders[signal["token_id"]] = {
            "question": mkt["question"],
            "attempted_at": time.time(),
        }

        if order:
            # Immediately place take-profit limit sell
            tp_price = round(price * (1 + config.SCALP_TAKE_PROFIT), 2)
            logger.info(f"  Placing TP sell at ${tp_price:.2f} (entry=${price:.2f} + {config.SCALP_TAKE_PROFIT:.0%})")
            try:
                self.executor.place_order(
                    token_id=signal["token_id"],
                    side="SELL",
                    price=tp_price,
                    size=num_shares,
                    order_type="GTC",
                )
            except Exception as e:
                logger.warning(f"  TP sell order failed: {e}")

            self._pending_orders[signal["token_id"]] = {
                "question": mkt["question"],
                "direction": signal["direction"],
                "price": price,
                "shares": num_shares,
                "placed_at": time.time(),
            }
            # Save to DB immediately so next tick sees it
            if config.DATABASE_URL:
                try:
                    positions = db.get_live_positions()
                    positions.append({
                        "question": mkt["question"],
                        "direction": signal["direction"],
                        "token_id": signal["token_id"],
                        "market_id": mkt.get("market_id", ""),
                        "entry_price": price,
                        "num_shares": num_shares,
                        "total_cost": round(price * num_shares, 2),
                        "current_value": round(price * num_shares, 2),
                        "cur_price": price,
                        "pnl": 0,
                        "source": "scalper",
                    })
                    db.save_live_positions(positions)
                except Exception:
                    pass
            return {
                "action": "entry",
                "type": signal["type"],
                "question": mkt["question"],
                "direction": signal["direction"],
                "price": price,
                "shares": num_shares,
                "reason": signal["reason"],
            }

        return None
