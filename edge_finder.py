"""
Edge Finder
Orchestrates market scanning, probability estimation, and opportunity ranking.
This is the "brain" that decides what to trade.
"""
import logging
from typing import Optional
from market_fetcher import MarketFetcher, Market
from probability_estimator import ProbabilityEstimator, ProbabilityEstimate
from risk_manager import RiskManager
import config
import db

logger = logging.getLogger(__name__)


class Opportunity:
    """A tradeable opportunity with sizing."""

    def __init__(
        self,
        estimate: ProbabilityEstimate,
        sizing: dict,
    ):
        self.estimate = estimate
        self.sizing = sizing
        self.market = estimate.market
        self.score = self._compute_score()

    def _compute_score(self) -> float:
        """
        Composite score combining edge quality, sizing, and market quality.
        Higher = better opportunity.
        """
        edge_score = self.estimate.effective_edge * 100
        confidence_bonus = self.estimate.confidence * 10

        # Prefer liquid markets
        liq = self.market.liquidity
        liq_score = min(liq / 100000, 1.0) * 5

        # Prefer medium time horizons (not too short, not too long)
        hours = self.market.hours_to_resolution or 500
        if 48 < hours < 720:  # 2-30 days sweet spot
            time_score = 5
        elif 24 < hours <= 48 or 720 <= hours < 1440:
            time_score = 2
        else:
            time_score = 0

        return edge_score + confidence_bonus + liq_score + time_score

    def to_dict(self) -> dict:
        return {
            "market_id": self.market.id,
            "question": self.market.question,
            "category": self.market.category,
            "market_price": round(self.market.implied_probability, 4),
            "estimated_prob": round(self.estimate.estimated_prob, 4),
            "edge": round(self.estimate.edge, 4),
            "effective_edge": round(self.estimate.effective_edge, 4),
            "direction": self.estimate.direction,
            "confidence": round(self.estimate.confidence, 4),
            "sizing": self.sizing,
            "score": round(self.score, 2),
            "reasoning": self.estimate.reasoning,
            "liquidity": self.market.liquidity,
            "volume_24h": self.market.volume_24h,
            "hours_to_resolution": self.market.hours_to_resolution,
        }

    def __repr__(self):
        return (
            f"Opportunity(score={self.score:.1f} | "
            f"{self.estimate.direction.upper()} {self.market.question[:40]}... | "
            f"Edge={self.estimate.edge_abs:.1%} | "
            f"Size=${self.sizing.get('total_cost', 0):.0f})"
        )


class EdgeFinder:
    """
    Scans markets and identifies tradeable opportunities.

    Pipeline:
    1. Fetch active markets
    2. Estimate probabilities for each
    3. Filter for sufficient edge
    4. Size positions via risk manager
    5. Rank by composite score
    """

    def __init__(
        self,
        fetcher: Optional[MarketFetcher] = None,
        estimator: Optional[ProbabilityEstimator] = None,
        risk_mgr: Optional[RiskManager] = None,
    ):
        self.fetcher = fetcher or MarketFetcher()
        self.estimator = estimator or ProbabilityEstimator()
        self.risk_mgr = risk_mgr or RiskManager()

    def scan(self, max_markets: int = None, on_progress=None) -> list[Opportunity]:
        """
        Full scan pipeline. Returns ranked list of opportunities.
        Only analyzes top N markets by liquidity to control LLM costs.
        """
        if max_markets is None:
            max_markets = config.LLM_MAX_MARKETS

        logger.info(f"Starting market scan (top {max_markets} by liquidity)...")

        # 1. Fetch markets, sorted by liquidity (most liquid first)
        all_markets = self.fetcher.fetch_active_markets(limit=200)
        all_markets.sort(key=lambda m: m.liquidity, reverse=True)
        markets = all_markets[:max_markets]
        logger.info(f"Analyzing {len(markets)} of {len(all_markets)} markets")

        if not markets:
            logger.warning("No markets found")
            return []

        # 2. Estimate probabilities
        estimates = self.estimator.batch_estimate(
            markets, on_progress=on_progress
        )
        logger.info(f"Found {len(estimates)} markets with potential edge")

        if not estimates:
            return []

        # 3. Size and filter through risk manager
        opportunities = []
        for est in estimates:
            sizing = self.risk_mgr.kelly_size(est)
            if sizing["approved"]:
                opp = Opportunity(estimate=est, sizing=sizing)
                opportunities.append(opp)
            else:
                logger.debug(
                    f"Rejected: {est.market.question[:50]} - {sizing.get('reason', 'unknown')}"
                )

        # 4. Sort by score
        opportunities.sort(key=lambda o: o.score, reverse=True)

        logger.info(
            f"Scan complete: {len(opportunities)} tradeable opportunities "
            f"from {len(markets)} markets"
        )

        # Log top opportunities
        for i, opp in enumerate(opportunities[:5]):
            logger.info(f"  #{i+1}: {opp}")

        return opportunities

    def get_top_opportunities(self, n: int = 5) -> list[Opportunity]:
        """Get the top N opportunities by score."""
        all_opps = self.scan()
        return all_opps[:n]

    def should_trade(self, opportunity: Opportunity, existing_positions: list = None) -> bool:
        """
        Final check before trading. Extra validation layer.
        """
        # Verify edge is still above threshold
        if opportunity.estimate.edge_abs < config.MIN_EDGE_THRESHOLD:
            return False

        # Verify sizing is still approved
        if not opportunity.sizing.get("approved"):
            return False

        # Verify risk manager isn't halted
        if self.risk_mgr.is_halted:
            return False

        # Verify market is still active
        if not opportunity.market.active or opportunity.market.closed:
            return False

        # Don't trade banned markets
        if config.DATABASE_URL:
            try:
                banned = db.get_banned_markets()
                if opportunity.market.id in banned or opportunity.market.condition_id in banned:
                    logger.info(f"Skipping banned market: {opportunity.market.question[:40]}")
                    return False
            except Exception:
                pass

        # Don't open duplicate positions in the same market
        if existing_positions:
            market_id = opportunity.market.id
            condition_id = opportunity.market.condition_id
            for pos in existing_positions:
                pos_market = pos.get("market_id", "")
                if pos_market == market_id or pos_market == condition_id:
                    logger.debug(f"Skipping {market_id[:20]} — already have a position")
                    return False

        return True
