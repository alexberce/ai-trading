"""
LLM-Based Probability Estimator
Uses Perplexity Sonar for real-time web context and Claude for calibrated forecasting.

Pipeline per market:
1. Query Perplexity Sonar with the market question → get web-grounded context
2. Feed context + market data to Claude → get calibrated probability estimate
3. Return structured signals for blending in probability_estimator.py
"""
import json
import time
import logging
import threading
from typing import Optional

import config

logger = logging.getLogger(__name__)

# Lazy-loaded clients
_anthropic_client = None
_perplexity_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _anthropic_client


def _get_perplexity_client():
    global _perplexity_client
    if _perplexity_client is None:
        from openai import OpenAI
        _perplexity_client = OpenAI(
            api_key=config.PERPLEXITY_API_KEY,
            base_url="https://api.perplexity.ai",
        )
    return _perplexity_client


# Thread-safe in-memory cache keyed by market ID, cleared each scan cycle
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


def clear_cache():
    """Clear the LLM estimate cache. Call at the start of each scan cycle."""
    with _cache_lock:
        _cache.clear()


def get_llm_signals(market) -> Optional[dict]:
    """
    Get LLM-based probability signals for a market.

    Returns dict with:
        llm_probability: float (0-1)
        llm_confidence: float (0-1)
        llm_reasoning: str
        web_context: str
    Or None if LLM estimation fails or is disabled.
    """
    if not config.LLM_ESTIMATION_ENABLED:
        return None

    with _cache_lock:
        if market.id in _cache:
            return _cache[market.id]

    try:
        # Step 1: Get web context from Perplexity
        web_context = _query_perplexity(market)

        # Step 2: Get calibrated estimate from Claude
        result = _query_claude(market, web_context)

        if result:
            result["web_context"] = web_context or ""
            with _cache_lock:
                _cache[market.id] = result
            return result

    except Exception as e:
        logger.warning(f"LLM estimation failed for {market.question[:50]}: {e}")

    return None


def _query_perplexity(market) -> Optional[str]:
    """Query Perplexity Sonar for real-time web context about the market."""
    if not config.PERPLEXITY_API_KEY:
        return None

    try:
        client = _get_perplexity_client()

        hours = market.hours_to_resolution
        time_context = f"This market resolves in {hours:.0f} hours." if hours else ""

        prompt = (
            f"What is the latest information relevant to this prediction market question? "
            f"Focus on recent news, data, and expert opinions.\n\n"
            f"Question: {market.question}\n"
            f"Description: {market.description[:500]}\n"
            f"Category: {market.category}\n"
            f"{time_context}\n\n"
            f"Provide a concise summary of the most relevant recent information. "
            f"Include specific facts, dates, and sources where possible."
        )

        response = client.chat.completions.create(
            model=config.PERPLEXITY_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
        )

        content = response.choices[0].message.content
        logger.debug(f"Perplexity context for {market.id}: {content[:100]}...")
        return content

    except Exception as e:
        logger.warning(f"Perplexity query failed for {market.question[:50]}: {e}")
        return None


def _query_claude(market, web_context: Optional[str] = None) -> Optional[dict]:
    """
    Query Claude for a calibrated probability estimate.
    Uses web context from Perplexity if available.
    """
    if not config.ANTHROPIC_API_KEY:
        return None

    try:
        client = _get_anthropic_client()

        hours = market.hours_to_resolution
        time_info = f"Time to resolution: {hours:.0f} hours" if hours else "No end date specified"

        context_block = ""
        if web_context:
            context_block = (
                f"\n## Recent Web Context\n"
                f"The following is recent information gathered from the web:\n"
                f"{web_context}\n"
            )

        prompt = f"""You are a calibrated probability forecaster. Estimate the probability that the following prediction market question resolves YES.

## Market Information
- Question: {market.question}
- Description: {market.description[:800]}
- Category: {market.category}
- Current market price (YES): {market.yes_price:.2f} (implies {market.implied_probability:.0%} probability)
- {time_info}
- Liquidity: ${market.liquidity:,.0f}
- 24h Volume: ${market.volume_24h:,.0f}
{context_block}
## Instructions
1. Consider base rates for this type of event
2. Account for the current market price — it reflects the consensus of many traders
3. Weigh recent information that the market may not have fully priced in
4. Be well-calibrated: when you say 70%, events should happen ~70% of the time
5. Avoid overconfidence — large deviations from market price require strong evidence

Respond with ONLY a JSON object (no markdown, no explanation outside the JSON):
{{"probability": <float 0.01-0.99>, "confidence": <float 0.1-0.9>, "reasoning": "<1-2 sentence explanation>"}}"""

        response = client.messages.create(
            model=config.LLM_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        # Handle potential markdown wrapping
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        data = json.loads(text)

        prob = float(data["probability"])
        conf = float(data["confidence"])
        reasoning = str(data.get("reasoning", ""))

        # Sanity checks
        prob = max(0.01, min(0.99, prob))
        conf = max(0.1, min(0.9, conf))

        logger.info(
            f"Claude estimate for {market.question[:50]}: "
            f"prob={prob:.0%} conf={conf:.0%} (market={market.implied_probability:.0%})"
        )

        return {
            "llm_probability": prob,
            "llm_confidence": conf,
            "llm_reasoning": reasoning,
        }

    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse Claude response for {market.question[:50]}: {e}")
        return None
    except Exception as e:
        logger.warning(f"Claude query failed for {market.question[:50]}: {e}")
        return None
