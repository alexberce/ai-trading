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
        existing_questions = {p.get("question", "").lower().strip() for p in existing}

        # Only count scalp-initiated positions (not LLM or manual)
        # For now, count all — but don't block if under total limit
        open_count = len(existing)
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

            change = (cur - entry) / entry if entry > 0 else 0
            reason = None

            # Take profit
            if change >= config.SCALP_TAKE_PROFIT:
                reason = f"TP hit: {change:+.1%} (entry={entry:.3f} now={cur:.3f})"

            # Stop loss
            elif change <= -config.SCALP_STOP_LOSS:
                reason = f"SL hit: {change:+.1%} (entry={entry:.3f} now={cur:.3f})"

            # Max hold time
            elif pos.get("opened_at"):
                try:
                    opened = datetime.fromisoformat(str(pos["opened_at"]).replace("Z", "+00:00"))
                    held_minutes = (datetime.now(timezone.utc) - opened).total_seconds() / 60
                    if held_minutes >= config.SCALP_MAX_HOLD_MINUTES:
                        reason = f"Max hold {held_minutes:.0f}min (limit={config.SCALP_MAX_HOLD_MINUTES})"
                except Exception:
                    pass

            if reason:
                logger.info(f"SCALP EXIT: {question[:40]} — {reason}")
                floor_price = round(max(cur * 0.95, 0.01), 2)
                try:
                    order = self.executor.place_market_order(
                        token_id=token_id,
                        side="SELL",
                        amount=shares,  # SELL: amount = shares
                        price=floor_price,  # Min acceptable price (5% slippage)
                    )
                    if order:
                        actions.append({
                            "action": "exit",
                            "question": question,
                            "reason": reason,
                            "entry": entry,
                            "exit": cur,
                            "pnl": round((cur - entry) * shares, 2),
                        })
                except Exception as e:
                    logger.error(f"Exit order failed: {e}")

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
                        "limit": 50, "order": "startDate", "ascending": "false",
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

                # Skip extreme prices (near 0 or 1)
                if yes_price < 0.05 or yes_price > 0.95:
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

            # Track prices for real-time change detection
            now_ts = time.time()
            for mkt in movers:
                mid = mkt["market_id"]
                price = mkt["yes_price"]
                if mid not in self._price_history:
                    self._price_history[mid] = []
                self._price_history[mid].append((now_ts, price))
                # Trim old entries
                self._price_history[mid] = [
                    (t, p) for t, p in self._price_history[mid]
                    if now_ts - t < self._HISTORY_WINDOW
                ]
                # Calculate changes at different timeframes
                history = self._price_history[mid]
                if len(history) >= 2:
                    # Change since last tick (real-time, ~5-30 seconds ago)
                    prev_price = history[-2][1]
                    mkt["tick_change"] = (price - prev_price) / prev_price if prev_price > 0 else 0

                    # Change over full window (~5 min)
                    oldest_price = history[0][1]
                    mkt["rt_change"] = (price - oldest_price) / oldest_price if oldest_price > 0 else 0
                else:
                    mkt["tick_change"] = 0
                    mkt["rt_change"] = 0

            # Sort by real-time change (most volatile), fall back to hourly
            movers.sort(key=lambda x: abs(x.get("rt_change", 0)) + abs(x["h_change"]), reverse=True)
            logger.info(f"Scalper: {len(movers)} active markets, {sum(1 for m in movers if abs(m.get('rt_change',0)) > 0.005)} with real-time movement")
            return movers

        except Exception as e:
            logger.error(f"Scalper fetch error: {e}")
            return []

    def _check_signal(self, mkt: dict) -> Optional[dict]:
        """Check if a market has a tradeable signal."""
        tick = mkt.get("tick_change", 0)  # Change since last tick (~5-30s)
        rt = mkt.get("rt_change", 0)  # Change over 5 min window
        h = mkt["h_change"]  # Hourly change
        d = mkt["d_change"]  # Daily change
        vol24 = mkt["vol24"]
        price = mkt["yes_price"]
        threshold = config.SCALP_MEAN_REVERSION_THRESHOLD

        # Instant tick signal (price just moved THIS tick)
        if abs(tick) >= threshold * 0.5:
            if tick < 0:
                return {
                    "type": "instant_dip",
                    "direction": "yes",
                    "token_id": mkt["yes_token"],
                    "price": price,
                    "reason": f"Just dropped {tick:+.2%} this tick, buying dip",
                }
            else:
                return {
                    "type": "instant_spike",
                    "direction": "no",
                    "token_id": mkt["no_token"],
                    "price": mkt["no_price"],
                    "reason": f"Just spiked {tick:+.2%} this tick, buying NO",
                }

        # Short-term signal (moved in last 5 min)
        if abs(rt) >= threshold:
            if rt < 0:
                return {
                    "type": "rt_dip",
                    "direction": "yes",
                    "token_id": mkt["yes_token"],
                    "price": price,
                    "reason": f"Dropped {rt:+.1%} in last 5min, buying dip",
                }
            else:
                return {
                    "type": "rt_spike",
                    "direction": "no",
                    "token_id": mkt["no_token"],
                    "price": mkt["no_price"],
                    "reason": f"Spiked {rt:+.1%} in last 5min, buying NO for pullback",
                }

        # Hourly mean reversion (price moved, expect correction)
        if abs(h) >= threshold * 2:
            if h < 0:
                return {
                    "type": "mean_reversion",
                    "direction": "yes",
                    "token_id": mkt["yes_token"],
                    "price": price,
                    "reason": f"YES dropped {h:+.1%} in 1h, expecting bounce",
                }
            else:
                return {
                    "type": "mean_reversion",
                    "direction": "no",
                    "token_id": mkt["no_token"],
                    "price": mkt["no_price"],
                    "reason": f"YES spiked {h:+.1%} in 1h, buying NO",
                }

        # Momentum: strong daily trend with volume
        if abs(d) > 0.03 and vol24 > 10000:
            if d > 0:
                return {
                    "type": "momentum",
                    "direction": "yes",
                    "token_id": mkt["yes_token"],
                    "price": price,
                    "reason": f"Momentum {d:+.1%} daily, vol ${vol24:,.0f}",
                }
            else:
                return {
                    "type": "momentum",
                    "direction": "no",
                    "token_id": mkt["no_token"],
                    "price": mkt["no_price"],
                    "reason": f"Momentum {d:+.1%} daily, vol ${vol24:,.0f}",
                }

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

        # Market order: spend $X, fill at best available price
        # Price is slippage protection — max 5% above current
        max_price = round(min(price * 1.05, 0.99), 2)
        order = self.executor.place_market_order(
            token_id=signal["token_id"],
            side="BUY",
            amount=spend,
            price=max_price,
        )
        num_shares = int(spend / price) if price > 0 else 0

        # Track attempt regardless of success to prevent retry spam
        self._pending_orders[signal["token_id"]] = {
            "question": mkt["question"],
            "attempted_at": time.time(),
        }

        if order:
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
