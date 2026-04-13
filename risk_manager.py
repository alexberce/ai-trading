"""
Risk Manager
Handles position sizing (Kelly criterion), exposure limits,
drawdown circuit breakers, and portfolio-level risk controls.
"""
import math
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from probability_estimator import ProbabilityEstimate
import config
import db

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Manages risk for the trading system.

    Key principles:
    1. Kelly criterion for sizing (with fractional Kelly for safety)
    2. Hard limits on per-position and total exposure
    3. Drawdown circuit breaker
    4. Category correlation limits
    """

    def __init__(self, bankroll: Optional[float] = None):
        self.initial_bankroll = bankroll or 0
        self.current_bankroll = self.initial_bankroll
        self.peak_bankroll = self.initial_bankroll
        self.daily_start_bankroll = self.initial_bankroll

        # Track positions
        self.open_positions: list[dict] = []
        self.closed_positions: list[dict] = []

        # Risk state
        self.is_halted = False
        self.halt_reason = ""

    def sync_bankroll(self, balance: float):
        """Sync bankroll from exchange balance, accounting for open position costs."""
        exposure = self._total_exposure()
        self.current_bankroll = balance + exposure
        if self.current_bankroll > self.peak_bankroll:
            self.peak_bankroll = self.current_bankroll
        logger.info(
            f"Bankroll synced: ${balance:.2f} available + "
            f"${exposure:.2f} deployed = ${self.current_bankroll:.2f} total"
        )

    # ── Kelly Criterion ───────────────────────────────────────────────

    def kelly_size(self, estimate: ProbabilityEstimate) -> dict:
        """
        Calculate optimal position size using Kelly criterion.

        Kelly fraction = (bp - q) / b
        where:
            b = odds received (payout ratio)
            p = estimated probability of winning
            q = 1 - p (probability of losing)

        We use fractional Kelly (default 25%) to reduce variance.
        """
        if self.is_halted:
            return self._reject("System halted", estimate)

        p = estimate.estimated_prob
        market_price = estimate.market_prob

        # Determine which side we're trading
        if estimate.direction == "yes":
            # Buying YES at market_price, pays $1 if correct
            cost = market_price
            payout = 1.0
        else:
            # Buying NO at (1 - market_price), pays $1 if correct
            cost = 1.0 - market_price
            p = 1.0 - p  # Flip probability for NO side
            payout = 1.0

        if cost <= 0 or cost >= 1:
            return self._reject("Invalid price", estimate)

        # Kelly formula for binary outcome
        b = (payout / cost) - 1  # Net odds
        q = 1.0 - p

        if b <= 0:
            return self._reject("Negative odds", estimate)

        full_kelly = (b * p - q) / b

        if full_kelly <= 0:
            return self._reject("Negative Kelly (no edge)", estimate)

        # Apply fractional Kelly
        fractional = full_kelly * config.MAX_KELLY_FRACTION
        dollar_size = fractional * self.current_bankroll

        # Apply hard limits
        max_position = self.current_bankroll * config.MAX_POSITION_PCT
        dollar_size = min(dollar_size, max_position)

        # Check total exposure
        current_exposure = self._total_exposure()
        max_new = (self.current_bankroll * config.MAX_TOTAL_EXPOSURE) - current_exposure
        if max_new <= 0:
            return self._reject("Max total exposure reached", estimate)
        dollar_size = min(dollar_size, max_new)

        # Check category correlation
        cat_count = sum(
            1 for pos in self.open_positions
            if pos.get("category") == estimate.market.category
        )
        if cat_count >= config.CORRELATION_LIMIT:
            return self._reject(
                f"Category limit reached ({estimate.market.category})", estimate
            )

        # Check max positions
        if len(self.open_positions) >= config.MAX_POSITIONS:
            return self._reject("Max positions reached", estimate)

        # Check daily loss limit
        daily_pnl = self.current_bankroll - self.daily_start_bankroll
        if daily_pnl < -(self.initial_bankroll * config.DAILY_LOSS_LIMIT_PCT):
            self._halt("Daily loss limit reached")
            return self._reject("Daily loss limit", estimate)

        # Check drawdown circuit breaker
        drawdown = (self.peak_bankroll - self.current_bankroll) / self.peak_bankroll
        if drawdown > config.MAX_DRAWDOWN_PCT:
            self._halt(f"Max drawdown exceeded ({drawdown:.1%})")
            return self._reject("Drawdown circuit breaker", estimate)

        # Calculate number of shares
        num_shares = math.floor(dollar_size / cost)
        if num_shares < 1:
            return self._reject("Position too small", estimate)

        actual_cost = num_shares * cost

        return {
            "approved": True,
            "direction": estimate.direction,
            "num_shares": num_shares,
            "cost_per_share": round(cost, 4),
            "total_cost": round(actual_cost, 2),
            "full_kelly_fraction": round(full_kelly, 4),
            "applied_fraction": round(fractional, 4),
            "pct_of_bankroll": round(actual_cost / self.current_bankroll, 4),
            "edge": round(estimate.edge_abs, 4),
            "confidence": round(estimate.confidence, 4),
        }

    def _reject(self, reason: str, estimate: ProbabilityEstimate) -> dict:
        logger.info(f"Position rejected for {estimate.market.question[:50]}: {reason}")
        return {
            "approved": False,
            "reason": reason,
            "direction": estimate.direction,
        }

    # ── Position Tracking ─────────────────────────────────────────────

    def open_position(self, estimate: ProbabilityEstimate, sizing: dict, order_id: str = ""):
        """Record a new open position."""
        position = {
            "market_id": estimate.market.id,
            "question": estimate.market.question,
            "category": estimate.market.category,
            "direction": sizing["direction"],
            "num_shares": sizing["num_shares"],
            "entry_price": sizing["cost_per_share"],
            "total_cost": sizing["total_cost"],
            "estimated_prob": estimate.estimated_prob,
            "market_prob_at_entry": estimate.market_prob,
            "edge_at_entry": estimate.edge,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "order_id": order_id,
            "token_id": (
                estimate.market.yes_token_id
                if sizing["direction"] == "yes"
                else estimate.market.no_token_id
            ),
        }
        self.open_positions.append(position)
        self.current_bankroll -= sizing["total_cost"]

        if config.DATABASE_URL:
            try:
                db.save_trade(position)
            except Exception as e:
                logger.error(f"DB save_trade failed: {e}")

        logger.info(
            f"Opened {sizing['direction'].upper()} position: "
            f"{sizing['num_shares']} shares @ ${sizing['cost_per_share']:.2f} "
            f"(${sizing['total_cost']:.2f}) on {estimate.market.question[:50]}"
        )

    def close_position(self, market_id: str, outcome: str, settlement_price: float):
        """Close a position when market resolves."""
        pos = None
        for p in self.open_positions:
            if p["market_id"] == market_id:
                pos = p
                break

        if not pos:
            logger.warning(f"No open position found for market {market_id}")
            return

        # Calculate P&L
        if pos["direction"] == "yes":
            pnl = pos["num_shares"] * (settlement_price - pos["entry_price"])
        else:
            pnl = pos["num_shares"] * ((1 - settlement_price) - pos["entry_price"])

        pos["closed_at"] = datetime.now(timezone.utc).isoformat()
        pos["settlement_price"] = settlement_price
        pos["outcome"] = outcome
        pos["pnl"] = round(pnl, 2)
        pos["return_pct"] = round(pnl / pos["total_cost"], 4) if pos["total_cost"] > 0 else 0

        self.closed_positions.append(pos)
        self.open_positions.remove(pos)
        self.current_bankroll += pos["total_cost"] + pnl

        # Update peak
        if self.current_bankroll > self.peak_bankroll:
            self.peak_bankroll = self.current_bankroll

        if config.DATABASE_URL:
            try:
                db.close_trade(market_id, outcome, settlement_price, pnl, pos["return_pct"])
            except Exception as e:
                logger.error(f"DB close_trade failed: {e}")

        logger.info(
            f"Closed position on {pos['question'][:50]}: "
            f"PnL=${pnl:+.2f} ({pos['return_pct']:+.0%})"
        )

    # ── Portfolio Stats ───────────────────────────────────────────────

    def _total_exposure(self) -> float:
        return sum(p["total_cost"] for p in self.open_positions)

    def reset_daily(self):
        """Reset daily tracking (call at start of each trading day)."""
        self.daily_start_bankroll = self.current_bankroll
        if not self.is_halted or "daily" in self.halt_reason.lower():
            self.is_halted = False
            self.halt_reason = ""

    def _halt(self, reason: str):
        self.is_halted = True
        self.halt_reason = reason
        logger.warning(f"TRADING HALTED: {reason}")

    def get_stats(self) -> dict:
        """Get current portfolio statistics."""
        total_exposure = self._total_exposure()
        total_pnl_closed = sum(p.get("pnl", 0) for p in self.closed_positions)
        drawdown = (
            (self.peak_bankroll - self.current_bankroll) / self.peak_bankroll
            if self.peak_bankroll > 0 else 0
        )

        wins = [p for p in self.closed_positions if p.get("pnl", 0) > 0]
        losses = [p for p in self.closed_positions if p.get("pnl", 0) <= 0]

        return {
            "bankroll": round(self.current_bankroll, 2),
            "initial_bankroll": self.initial_bankroll,
            "total_return": round(
                (self.current_bankroll - self.initial_bankroll) / self.initial_bankroll, 4
            ) if self.initial_bankroll > 0 else 0,
            "total_pnl_closed": round(total_pnl_closed, 2),
            "peak_bankroll": round(self.peak_bankroll, 2),
            "drawdown": round(drawdown, 4),
            "open_positions": len(self.open_positions),
            "total_exposure": round(total_exposure, 2),
            "exposure_pct": round(total_exposure / self.current_bankroll, 4)
            if self.current_bankroll > 0 else 0,
            "closed_trades": len(self.closed_positions),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(self.closed_positions), 4)
            if self.closed_positions else 0,
            "avg_win": round(
                sum(p["pnl"] for p in wins) / len(wins), 2
            ) if wins else 0,
            "avg_loss": round(
                sum(p["pnl"] for p in losses) / len(losses), 2
            ) if losses else 0,
            "is_halted": self.is_halted,
            "halt_reason": self.halt_reason,
        }

    # ── Persistence ───────────────────────────────────────────────────

    def save_state(self, filepath: str = ""):
        """Save risk manager state to DB (preferred) or disk."""
        state = {
            "bankroll": self.current_bankroll,
            "peak_bankroll": self.peak_bankroll,
            "daily_start": self.daily_start_bankroll,
            "open_positions": self.open_positions,
            "closed_positions": self.closed_positions[-50:],  # Keep last 50
            "is_halted": self.is_halted,
            "halt_reason": self.halt_reason,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }

        if config.DATABASE_URL:
            try:
                db.save_state("risk_manager", state)
                return
            except Exception as e:
                logger.error(f"DB save_state failed, falling back to file: {e}")

        filepath = filepath or config.STATE_FILE
        with open(filepath, "w") as f:
            json.dump(state, f, indent=2)

    def load_state(self, filepath: str = ""):
        """Load risk manager state from DB (preferred) or disk."""
        state = None

        if config.DATABASE_URL:
            try:
                db.init_db()
                state = db.load_state("risk_manager")
                if state:
                    logger.info("Loaded state from database")
            except Exception as e:
                logger.error(f"DB load_state failed, falling back to file: {e}")

        if state is None:
            filepath = filepath or config.STATE_FILE
            try:
                with open(filepath) as f:
                    state = json.load(f)
                logger.info("Loaded state from file")
            except FileNotFoundError:
                logger.info("No saved state found, starting fresh")
                return
            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"Failed to load state: {e}")
                return

        try:
            self.current_bankroll = state["bankroll"]
            self.peak_bankroll = state["peak_bankroll"]
            self.daily_start_bankroll = state["daily_start"]
            self.open_positions = state["open_positions"]
            self.closed_positions = state["closed_positions"]
            self.is_halted = state.get("is_halted", False)
            self.halt_reason = state.get("halt_reason", "")
            logger.info(f"Loaded state: bankroll=${self.current_bankroll:.2f}, "
                        f"{len(self.open_positions)} open positions")
        except KeyError as e:
            logger.error(f"Invalid state data: {e}")
