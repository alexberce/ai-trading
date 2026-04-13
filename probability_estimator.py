"""
Probability Estimator
Estimates "fair" probabilities for prediction market outcomes.

This is where the edge comes from. The estimator uses:
1. Market price as a Bayesian prior
2. Base rate analysis for event categories
3. Favorite-longshot bias correction
4. Time decay modeling
5. Sentiment signals (when available)

The key insight: markets are populated by humans with systematic biases.
This module tries to correct for those biases methodically.
"""
import math
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional
from market_fetcher import Market
import config
import llm_estimator

logger = logging.getLogger(__name__)


class ProbabilityEstimate:
    """Holds a probability estimate with confidence and reasoning."""

    def __init__(
        self,
        market: Market,
        estimated_prob: float,
        confidence: float,
        components: dict,
        reasoning: str,
    ):
        self.market = market
        self.estimated_prob = max(0.01, min(0.99, estimated_prob))
        self.confidence = confidence  # 0-1, how confident we are in our estimate
        self.components = components  # Breakdown of estimation factors
        self.reasoning = reasoning
        self.market_prob = market.implied_probability
        self.edge = self.estimated_prob - self.market_prob
        self.edge_abs = abs(self.edge)

    @property
    def has_edge(self) -> bool:
        """Whether this estimate suggests a tradeable edge."""
        return self.edge_abs >= config.MIN_EDGE_THRESHOLD

    @property
    def direction(self) -> str:
        """Which side to trade: 'yes' if underpriced, 'no' if overpriced."""
        return "yes" if self.edge > 0 else "no"

    @property
    def effective_edge(self) -> float:
        """Edge adjusted for confidence."""
        return self.edge_abs * self.confidence

    def to_dict(self) -> dict:
        return {
            "market_id": self.market.id,
            "question": self.market.question,
            "market_prob": round(self.market_prob, 4),
            "estimated_prob": round(self.estimated_prob, 4),
            "edge": round(self.edge, 4),
            "edge_abs": round(self.edge_abs, 4),
            "effective_edge": round(self.effective_edge, 4),
            "confidence": round(self.confidence, 4),
            "direction": self.direction,
            "has_edge": self.has_edge,
            "components": self.components,
            "reasoning": self.reasoning,
        }

    def __repr__(self):
        arrow = "▲" if self.edge > 0 else "▼"
        return (
            f"Estimate({self.market.question[:50]}... | "
            f"Mkt={self.market_prob:.0%} → Est={self.estimated_prob:.0%} "
            f"{arrow} Edge={self.edge_abs:.1%})"
        )


class ProbabilityEstimator:
    """
    Estimates fair probabilities for prediction markets.

    The estimation pipeline:
    1. Start with market price as base (it's the best public estimate)
    2. Apply bias corrections (favorite-longshot, recency)
    3. Model time decay effects
    4. Incorporate any external signals
    5. Blend components with configurable weights
    """

    def __init__(self):
        self.category_configs = config.CATEGORY_CONFIGS

    def estimate(self, market: Market, external_signals: Optional[dict] = None) -> ProbabilityEstimate:
        """
        Produce a probability estimate for a market.

        Args:
            market: The market to estimate
            external_signals: Optional dict with keys like:
                - "sentiment_score": float (-1 to 1)
                - "related_market_prices": list of related market prices
                - "news_count": number of recent news articles
                - "expert_forecast": float probability from expert source
        """
        market_prob = market.implied_probability
        cat_config = self.category_configs.get(
            market.category,
            self.category_configs["default"]
        )

        components = {}

        # ── Component 1: Favorite-Longshot Bias Correction ──────────
        flb_adjusted = self._correct_favorite_longshot(
            market_prob,
            bias_strength=cat_config["favorite_longshot_bias"],
        )
        components["flb_correction"] = flb_adjusted

        # ── Component 2: Time Decay Modeling ────────────────────────
        time_adjusted = self._model_time_decay(market)
        components["time_decay"] = time_adjusted

        # ── Component 3: Liquidity Signal ───────────────────────────
        liquidity_adj = self._liquidity_adjustment(market)
        components["liquidity_signal"] = liquidity_adj

        # ── Component 4: Volume Momentum ────────────────────────────
        volume_signal = self._volume_momentum(market)
        components["volume_momentum"] = volume_signal

        # ── Component 5: External Signals (if available) ────────────
        external_adj = 0.0
        if external_signals:
            external_adj = self._process_external_signals(
                market_prob, external_signals
            )
        components["external_signals"] = external_adj

        # ── Component 6: LLM-Based Estimation ──────────────────────
        llm_signals = llm_estimator.get_llm_signals(market)
        components["llm_signals"] = llm_signals

        # ── Blend All Components ────────────────────────────────────
        # Start with market price, apply adjustments
        estimated = market_prob

        has_llm = llm_signals is not None
        # When LLM signals are available, reduce statistical weights
        # to make room for the LLM component
        stat_scale = (1.0 - config.LLM_ESTIMATE_WEIGHT) if has_llm else 1.0

        # FLB correction is the primary adjustment
        estimated += (flb_adjusted - market_prob) * 0.4 * stat_scale

        # Time decay
        estimated += time_adjusted * 0.15 * stat_scale

        # Liquidity adjustment (thin markets are less reliable)
        estimated += liquidity_adj * 0.1 * stat_scale

        # Volume momentum
        estimated += volume_signal * 0.1 * stat_scale

        # External signals get strong weight when available
        if external_signals:
            estimated += external_adj * 0.25 * stat_scale

        # LLM signal: blend toward the LLM probability estimate
        if has_llm:
            llm_prob = llm_signals["llm_probability"]
            llm_conf = llm_signals["llm_confidence"]
            # Weight scales with both the configured weight and the LLM's confidence
            llm_weight = config.LLM_ESTIMATE_WEIGHT * llm_conf
            estimated = estimated * (1 - llm_weight) + llm_prob * llm_weight

        # Clamp to valid range
        estimated = max(0.02, min(0.98, estimated))

        # ── Calculate Confidence ────────────────────────────────────
        confidence = self._calculate_confidence(market, external_signals)

        # ── Generate Reasoning ──────────────────────────────────────
        reasoning = self._generate_reasoning(
            market, market_prob, estimated, components, confidence
        )

        return ProbabilityEstimate(
            market=market,
            estimated_prob=estimated,
            confidence=confidence,
            components=components,
            reasoning=reasoning,
        )

    def _correct_favorite_longshot(self, prob: float, bias_strength: float) -> float:
        """
        Correct for favorite-longshot bias.

        In prediction markets, longshots (low probability events) tend to be
        overpriced and favorites (high probability events) tend to be underpriced.

        This shifts probabilities toward 50% for longshots and away for favorites,
        effectively "flattening" the extremes.
        """
        if prob < 0.2:
            # Longshot: market likely overprices this
            correction = -bias_strength * (0.2 - prob) / 0.2
            return prob + correction
        elif prob > 0.8:
            # Favorite: market likely underprices this
            correction = bias_strength * (prob - 0.8) / 0.2
            return prob + correction
        else:
            # Middle range: minimal bias
            return prob

    def _model_time_decay(self, market: Market) -> float:
        """
        Model how probability should shift based on time to resolution.

        Key insight: as resolution approaches with no change in fundamentals,
        the probability should drift toward the current price more strongly.
        Far-out markets have more uncertainty and room for movement.
        """
        hours = market.hours_to_resolution
        if hours is None:
            return 0.0

        prob = market.implied_probability

        if hours < 48:
            # Very close to resolution - strong mean reversion to current price
            # Markets that haven't moved are likely correctly priced
            return 0.0
        elif hours < 168:  # 1 week
            # Moderate time - slight adjustment toward 50% (uncertainty)
            return (0.5 - prob) * 0.03
        else:
            # Far out - more uncertainty, slight pull toward 50%
            return (0.5 - prob) * 0.05

    def _liquidity_adjustment(self, market: Market) -> float:
        """
        Less liquid markets are more likely to be mispriced.
        Returns a small adjustment toward 50% for illiquid markets
        (reflecting greater uncertainty about true price).
        """
        prob = market.implied_probability
        liq = market.liquidity

        if liq < 10000:
            # Thin market - pull slightly toward 50%
            return (0.5 - prob) * 0.05
        elif liq < 50000:
            return (0.5 - prob) * 0.02
        else:
            return 0.0

    def _volume_momentum(self, market: Market) -> float:
        """
        High recent volume may indicate new information.
        If volume is unusually high, the current price is more likely
        to reflect new info rather than stale bias.
        """
        if market.volume_24h > market.liquidity * 0.5:
            # Very high volume relative to liquidity
            # Trust the market more (reduce our adjustment)
            return 0.0
        return 0.0  # Neutral by default

    def _process_external_signals(self, market_prob: float, signals: dict) -> float:
        """Process external signals into a probability adjustment."""
        adj = 0.0

        # Sentiment score (-1 to 1)
        sentiment = signals.get("sentiment_score", 0)
        if sentiment != 0:
            adj += sentiment * 0.05

        # Expert forecast
        expert = signals.get("expert_forecast")
        if expert is not None:
            # Strong signal: expert disagrees with market
            adj += (expert - market_prob) * 0.3

        return adj

    def _calculate_confidence(self, market: Market, external: Optional[dict]) -> float:
        """
        How confident are we in our estimate?
        Higher confidence = willing to bet more.
        """
        conf = 0.5  # Base confidence

        # More liquid markets → more confident in our read
        if market.liquidity > 100000:
            conf += 0.1
        elif market.liquidity > 50000:
            conf += 0.05

        # Markets closer to resolution are harder to disagree with
        hours = market.hours_to_resolution
        if hours is not None:
            if hours < 24:
                conf -= 0.2
            elif hours < 72:
                conf -= 0.1

        # External signals boost confidence
        if external:
            if "expert_forecast" in external:
                conf += 0.15
            if "sentiment_score" in external:
                conf += 0.05

        # Well-known categories we can analyze better
        if market.category in ("politics", "sports"):
            conf += 0.05

        # LLM signals boost confidence significantly
        llm = llm_estimator.get_llm_signals(market)
        if llm:
            conf += 0.10 * llm["llm_confidence"]

        return max(0.1, min(0.9, conf))

    def _generate_reasoning(
        self,
        market: Market,
        market_prob: float,
        estimated: float,
        components: dict,
        confidence: float,
    ) -> str:
        """Generate human-readable reasoning for the estimate."""
        parts = []
        edge = estimated - market_prob

        if abs(edge) < 0.03:
            parts.append(f"Market price of {market_prob:.0%} appears roughly fair.")
        elif edge > 0:
            parts.append(
                f"Market at {market_prob:.0%} appears to UNDERPRICE this outcome. "
                f"Estimate: {estimated:.0%}."
            )
        else:
            parts.append(
                f"Market at {market_prob:.0%} appears to OVERPRICE this outcome. "
                f"Estimate: {estimated:.0%}."
            )

        # Explain key drivers
        flb = components.get("flb_correction", market_prob)
        if abs(flb - market_prob) > 0.02:
            if market_prob < 0.2:
                parts.append("Favorite-longshot bias correction: longshot likely overpriced.")
            elif market_prob > 0.8:
                parts.append("Favorite-longshot bias correction: favorite likely underpriced.")

        if components.get("liquidity_signal", 0) != 0:
            parts.append("Thin liquidity suggests potential mispricing.")

        # LLM reasoning
        llm = components.get("llm_signals")
        if llm:
            parts.append(f"LLM estimate: {llm['llm_probability']:.0%} "
                         f"(conf {llm['llm_confidence']:.0%}). "
                         f"{llm.get('llm_reasoning', '')}")

        parts.append(f"Confidence: {confidence:.0%}. Category: {market.category}.")

        return " ".join(parts)

    def batch_estimate(
        self,
        markets: list[Market],
        min_edge: Optional[float] = None,
        on_progress: Optional[callable] = None,
    ) -> list[ProbabilityEstimate]:
        """
        Estimate probabilities for a batch of markets.
        Returns estimates sorted by edge (largest first).

        Args:
            on_progress: Optional callback(done, total, estimates_so_far)
                         called after each market is processed.
        """
        if min_edge is None:
            min_edge = config.MIN_EDGE_THRESHOLD

        llm_estimator.clear_cache()
        estimates = []
        done_count = 0
        total = len(markets)

        # Pre-fetch LLM signals in parallel (the slow part)
        if config.LLM_ESTIMATION_ENABLED:
            max_workers = min(8, total)
            logger.info(f"Pre-fetching LLM signals for {total} markets ({max_workers} workers)...")
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(llm_estimator.get_llm_signals, m): m
                    for m in markets
                }
                for future in as_completed(futures):
                    done_count += 1
                    market = futures[future]
                    try:
                        future.result()  # Result is cached in llm_estimator
                    except Exception as e:
                        logger.error(f"LLM prefetch failed for {market.id}: {e}")

                    # Estimate this market now that its LLM signal is cached
                    try:
                        est = self.estimate(market)
                        if est.edge_abs >= min_edge:
                            estimates.append(est)
                    except Exception as e:
                        logger.error(f"Failed to estimate {market.id}: {e}")

                    if on_progress:
                        try:
                            on_progress(done_count, total, estimates)
                        except Exception:
                            pass
        else:
            # No LLM — run sequentially (fast)
            for i, market in enumerate(markets):
                try:
                    est = self.estimate(market)
                    if est.edge_abs >= min_edge:
                        estimates.append(est)
                except Exception as e:
                    logger.error(f"Failed to estimate {market.id}: {e}")

                if on_progress:
                    try:
                        on_progress(i + 1, total, estimates)
                    except Exception:
                        pass

        estimates.sort(key=lambda e: e.effective_edge, reverse=True)
        logger.info(
            f"Found {len(estimates)} opportunities with edge >= {min_edge:.0%} "
            f"from {len(markets)} markets"
        )

        return estimates
