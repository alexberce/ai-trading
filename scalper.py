"""
Scalper — Short-Term Trading Strategy
Buy → wait for small price move → sell for $2-$10 profit.

Two strategies running in parallel:
1. Mean Reversion: buy when price dips below recent average, sell when it reverts
2. Momentum: ride volume spikes in the direction of the move

Runs every 30 seconds. Targets 10min-3h hold times.
"""
import time
import logging
from datetime import datetime, timezone
from typing import Optional

from market_fetcher import MarketFetcher, Market
from executor import Executor
from risk_manager import RiskManager
import config

logger = logging.getLogger(__name__)


class ScalpPosition:
    """Tracks a single scalp trade."""

    def __init__(self, market: Market, token_id: str, direction: str,
                 entry_price: float, num_shares: int, order_id: str = ""):
        self.market = market
        self.token_id = token_id
        self.direction = direction
        self.entry_price = entry_price
        self.num_shares = num_shares
        self.order_id = order_id
        self.opened_at = time.time()
        self.target_price = entry_price + config.SCALP_TAKE_PROFIT
        self.stop_price = entry_price - config.SCALP_STOP_LOSS

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.opened_at) / 60

    @property
    def should_force_exit(self) -> bool:
        return self.age_minutes >= config.SCALP_MAX_HOLD_MINUTES

    def check_exit(self, current_price: float) -> Optional[str]:
        """Check if position should be exited. Returns reason or None."""
        if current_price >= self.target_price:
            return "take_profit"
        if current_price <= self.stop_price:
            return "stop_loss"
        if self.should_force_exit:
            return "max_hold_time"
        return None

    def to_dict(self) -> dict:
        return {
            "question": self.market.question,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "target_price": self.target_price,
            "stop_price": self.stop_price,
            "num_shares": self.num_shares,
            "age_minutes": round(self.age_minutes, 1),
            "token_id": self.token_id,
            "market_id": self.market.id,
            "position_type": "scalp",
        }


class Scalper:
    """
    Short-term trading engine.

    Tick cycle (every 30s):
    1. Monitor open scalp positions — exit if target/stop/timeout hit
    2. Scan for mean reversion opportunities — price below recent average
    3. Scan for momentum opportunities — volume spike + directional move
    4. Enter new positions if conditions met
    """

    def __init__(self, fetcher: MarketFetcher, executor: Executor, risk_mgr: RiskManager):
        self.fetcher = fetcher
        self.executor = executor
        self.risk_mgr = risk_mgr
        self.positions: list[ScalpPosition] = []
        self._price_history: dict[str, list[float]] = {}  # token_id -> recent prices

    def tick(self) -> list[dict]:
        """
        Run one scalp cycle. Returns list of actions taken.
        Called every SCALP_SCAN_INTERVAL seconds.
        """
        actions = []

        # 1. Monitor and exit positions
        for pos in list(self.positions):
            try:
                mid = self.fetcher.get_midpoint(pos.token_id)
                if mid is None:
                    continue

                exit_reason = pos.check_exit(mid)
                if exit_reason:
                    pnl = (mid - pos.entry_price) * pos.num_shares
                    action = self._exit_position(pos, mid, exit_reason)
                    if action:
                        actions.append(action)
            except Exception as e:
                logger.error(f"Scalp monitor error for {pos.market.question[:30]}: {e}")

        # 2. Look for new entries (only if we have capacity)
        if len(self.positions) < config.SCALP_MAX_CONCURRENT:
            try:
                markets = self.fetcher.fetch_active_markets(limit=50)
                liquid_markets = [
                    m for m in markets
                    if m.liquidity >= config.SCALP_MIN_LIQUIDITY
                    and m.yes_token_id
                    and not self._already_in(m)
                ]
                logger.info(f"Scalp tick: {len(liquid_markets)} liquid markets, "
                            f"{len(self.positions)}/{config.SCALP_MAX_CONCURRENT} positions open")

                for market in liquid_markets[:10]:  # Check top 10 liquid markets
                    if len(self.positions) >= config.SCALP_MAX_CONCURRENT:
                        break

                    signal = self._find_entry_signal(market)
                    if signal:
                        action = self._enter_position(market, signal)
                        if action:
                            actions.append(action)

            except Exception as e:
                logger.error(f"Scalp scan error: {e}")

        return actions

    def _find_entry_signal(self, market: Market) -> Optional[dict]:
        """
        Check if market has a scalp entry signal.
        Returns signal dict or None.
        """
        token_id = market.yes_token_id
        mid = self.fetcher.get_midpoint(token_id)
        if mid is None or mid <= 0:
            return None

        # Track price history
        history = self._price_history.setdefault(token_id, [])
        history.append(mid)
        if len(history) > 20:
            history.pop(0)

        # Need at least 5 data points
        if len(history) < 5:
            return None

        avg = sum(history) / len(history)
        deviation = (mid - avg) / avg if avg > 0 else 0

        # ── Mean Reversion: price dipped below average ──
        if deviation < -config.SCALP_MEAN_REVERSION_THRESHOLD:
            return {
                "type": "mean_reversion",
                "direction": "yes",
                "token_id": token_id,
                "price": mid,
                "avg_price": avg,
                "deviation": deviation,
                "reason": f"Price {mid:.3f} is {abs(deviation):.1%} below avg {avg:.3f}",
            }

        # ── Mean Reversion: price spiked above average (buy NO) ──
        if deviation > config.SCALP_MEAN_REVERSION_THRESHOLD:
            no_token = market.no_token_id
            no_mid = self.fetcher.get_midpoint(no_token) if no_token else None
            if no_mid and no_mid > 0:
                return {
                    "type": "mean_reversion",
                    "direction": "no",
                    "token_id": no_token,
                    "price": no_mid,
                    "avg_price": 1 - avg,
                    "deviation": -deviation,
                    "reason": f"YES price {mid:.3f} is {deviation:.1%} above avg, buying NO",
                }

        # ── Momentum: check for volume spike ──
        spread_info = self.fetcher.get_spread(token_id)
        if spread_info and spread_info["spread"] < 0.02:
            # Tight spread + price moving up = momentum
            if len(history) >= 3 and all(history[-i] > history[-i-1] for i in range(1, min(3, len(history)))):
                return {
                    "type": "momentum",
                    "direction": "yes",
                    "token_id": token_id,
                    "price": mid,
                    "reason": f"Tight spread ({spread_info['spread']:.3f}) + upward momentum",
                }

        return None

    def _enter_position(self, market: Market, signal: dict) -> Optional[dict]:
        """Place a buy order for a scalp position."""
        price = signal["price"]
        max_shares = int(config.SCALP_MAX_POSITION_SIZE / price) if price > 0 else 0
        if max_shares < 1:
            return None

        order = self.executor.execute_trade(
            token_id=signal["token_id"],
            direction=signal["direction"],
            num_shares=max_shares,
            target_price=price,
        )

        if order:
            pos = ScalpPosition(
                market=market,
                token_id=signal["token_id"],
                direction=signal["direction"],
                entry_price=price,
                num_shares=max_shares,
                order_id=order.id,
            )
            self.positions.append(pos)
            logger.info(
                f"SCALP ENTRY: {signal['type']} {signal['direction'].upper()} "
                f"{market.question[:40]} @ {price:.3f} x{max_shares} "
                f"(target={pos.target_price:.3f} stop={pos.stop_price:.3f})"
            )
            return {
                "action": "entry",
                "type": signal["type"],
                "question": market.question,
                "direction": signal["direction"],
                "price": price,
                "shares": max_shares,
                "reason": signal["reason"],
            }

        return None

    def _exit_position(self, pos: ScalpPosition, current_price: float,
                       reason: str) -> Optional[dict]:
        """Exit a scalp position."""
        pnl = (current_price - pos.entry_price) * pos.num_shares

        # Place sell order
        order = self.executor.place_limit_order(
            token_id=pos.token_id,
            side="SELL",
            price=round(current_price, 2),
            size=pos.num_shares,
        )

        self.positions.remove(pos)

        logger.info(
            f"SCALP EXIT ({reason}): {pos.direction.upper()} "
            f"{pos.market.question[:40]} @ {current_price:.3f} "
            f"(entry={pos.entry_price:.3f} pnl=${pnl:+.2f} held={pos.age_minutes:.0f}min)"
        )

        return {
            "action": "exit",
            "reason": reason,
            "question": pos.market.question,
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "exit_price": current_price,
            "pnl": round(pnl, 2),
            "hold_minutes": round(pos.age_minutes, 1),
        }

    def _already_in(self, market: Market) -> bool:
        """Check if we already have a scalp position in this market."""
        return any(
            p.market.id == market.id for p in self.positions
        )

    def get_positions_dicts(self) -> list[dict]:
        """Get all scalp positions as dicts for the dashboard."""
        result = []
        for pos in self.positions:
            d = pos.to_dict()
            mid = self.fetcher.get_midpoint(pos.token_id)
            if mid:
                d["current_price"] = mid
                d["unrealized_pnl"] = round((mid - pos.entry_price) * pos.num_shares, 2)
            result.append(d)
        return result
