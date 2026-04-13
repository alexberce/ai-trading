"""
Market Fetcher
Pulls active markets from Polymarket's Gamma API and CLOB API.
Filters for tradeable markets with sufficient liquidity.
"""
import time
import json
import logging
import requests
from datetime import datetime, timezone
from typing import Optional
import config

logger = logging.getLogger(__name__)


class Market:
    """Represents a single prediction market."""

    def __init__(self, raw_data: dict):
        self.raw = raw_data
        self.id = raw_data.get("id", "")
        self.condition_id = raw_data.get("condition_id", "")
        self.question = raw_data.get("question", "")
        self.description = raw_data.get("description", "")
        self.category = self._extract_category(raw_data)
        self.end_date = raw_data.get("end_date_iso", "")
        self.start_date = raw_data.get("startDateIso", "") or raw_data.get("startDate", "")
        self.created_at = raw_data.get("createdAt", "")
        self.active = raw_data.get("active", False)
        self.closed = raw_data.get("closed", False)
        self.volume = float(raw_data.get("volume", 0) or 0)
        self.volume_24h = float(raw_data.get("volume_24hr", 0) or 0)
        self.liquidity = float(raw_data.get("liquidity", 0) or 0)

        # Token info for YES/NO outcomes
        # Gamma API uses outcomePrices + clobTokenIds instead of tokens[]
        self.tokens = raw_data.get("tokens", [])
        self.yes_token_id = ""
        self.no_token_id = ""
        self.yes_price = 0.0
        self.no_price = 0.0
        self._parse_tokens()

    def _extract_category(self, data: dict) -> str:
        tags = data.get("tags", [])
        if tags:
            tag = tags[0].lower() if isinstance(tags[0], str) else str(tags[0]).lower()
            for cat in config.CATEGORY_CONFIGS:
                if cat in tag:
                    return cat
        return "default"

    def _parse_tokens(self):
        # Try legacy tokens[] format first
        for token in self.tokens:
            outcome = token.get("outcome", "").lower()
            if outcome == "yes":
                self.yes_token_id = token.get("token_id", "")
                self.yes_price = float(token.get("price", 0))
            elif outcome == "no":
                self.no_token_id = token.get("token_id", "")
                self.no_price = float(token.get("price", 0))

        # Fall back to outcomePrices + clobTokenIds (current Gamma API format)
        if not self.yes_token_id:
            outcomes = self.raw.get("outcomes", [])
            prices = self.raw.get("outcomePrices", [])
            token_ids = self.raw.get("clobTokenIds", [])

            # These fields can be JSON strings instead of lists
            if isinstance(outcomes, str):
                try: outcomes = json.loads(outcomes)
                except: outcomes = []
            if isinstance(prices, str):
                try: prices = json.loads(prices)
                except: prices = []
            if isinstance(token_ids, str):
                try: token_ids = json.loads(token_ids)
                except: token_ids = []

            if outcomes and prices and token_ids and len(outcomes) >= 2:
                for i, outcome in enumerate(outcomes):
                    if i >= len(prices) or i >= len(token_ids):
                        break
                    price = float(prices[i]) if prices[i] else 0
                    tid = token_ids[i] if token_ids[i] else ""
                    if outcome.lower() == "yes":
                        self.yes_token_id = tid
                        self.yes_price = price
                    elif outcome.lower() == "no":
                        self.no_token_id = tid
                        self.no_price = price

                # If outcomes aren't "Yes"/"No" (e.g. team names), treat first as yes
                if not self.yes_token_id and len(token_ids) >= 2:
                    self.yes_token_id = token_ids[0]
                    self.yes_price = float(prices[0]) if prices[0] else 0
                    self.no_token_id = token_ids[1]
                    self.no_price = float(prices[1]) if prices[1] else 0

    @property
    def implied_probability(self) -> float:
        """Market's implied probability from YES price."""
        return self.yes_price

    @property
    def hours_to_resolution(self) -> Optional[float]:
        """Hours until market resolves."""
        if not self.end_date:
            return None
        try:
            end = datetime.fromisoformat(self.end_date.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = (end - now).total_seconds() / 3600
            return max(0, delta)
        except (ValueError, TypeError):
            return None

    def passes_filters(self) -> bool:
        """Check if market passes all configurable filters."""
        if not self.active or self.closed:
            return False

        if self.liquidity < config.MIN_LIQUIDITY_USD:
            return False

        # Use volume_24h if available, otherwise use total volume
        effective_volume = self.volume_24h if self.volume_24h > 0 else self.volume
        if effective_volume < config.MIN_VOLUME_24H:
            return False

        if self.yes_price < config.MIN_PRICE or self.yes_price > config.MAX_PRICE:
            return False

        hours = self.hours_to_resolution
        if hours is not None:
            if hours < config.MIN_TIME_TO_RESOLUTION_HOURS:
                return False
            if hours > config.MAX_TIME_TO_RESOLUTION_DAYS * 24:
                return False

        if not self.yes_token_id:
            return False

        return True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "condition_id": self.condition_id,
            "question": self.question,
            "category": self.category,
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "yes_token_id": self.yes_token_id,
            "no_token_id": self.no_token_id,
            "volume_24h": self.volume_24h,
            "liquidity": self.liquidity,
            "hours_to_resolution": self.hours_to_resolution,
            "end_date": self.end_date,
        }

    def __repr__(self):
        return f"Market({self.question[:60]}... | YES={self.yes_price:.2f} | Liq=${self.liquidity:,.0f})"


class MarketFetcher:
    """Fetches and filters markets from Polymarket."""

    def __init__(self):
        self.gamma_base = config.POLYMARKET_GAMMA_API
        self.clob_base = config.POLYMARKET_API_BASE
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "PredictionTrader/1.0",
        })
        self._cache = {}
        self._cache_time = 0

    def fetch_active_markets(self, limit: int = 200, use_cache: bool = True) -> list[Market]:
        """
        Fetch active markets from Polymarket Gamma API.
        Returns filtered list of Market objects.
        """
        cache_age = time.time() - self._cache_time
        if use_cache and self._cache and cache_age < 60:
            return self._cache.get("markets", [])

        markets = []
        offset = 0
        page_size = 100

        while offset < limit:
            try:
                params = {
                    "active": "true",
                    "closed": "false",
                    "limit": min(page_size, limit - offset),
                    "offset": offset,
                    "order": "volume_24hr",
                    "ascending": "false",
                }
                resp = self.session.get(
                    f"{self.gamma_base}/markets",
                    params=params,
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()

                if not data:
                    break

                for raw in data:
                    market = Market(raw)
                    if market.passes_filters():
                        markets.append(market)

                offset += page_size
                time.sleep(0.5)  # Rate limiting

            except requests.RequestException as e:
                logger.error(f"Failed to fetch markets (offset={offset}): {e}")
                break

        logger.info(f"Fetched {len(markets)} tradeable markets from {offset} total scanned")

        self._cache["markets"] = markets
        self._cache_time = time.time()

        return markets

    def get_orderbook(self, token_id: str) -> dict:
        """Fetch orderbook for a specific token."""
        try:
            resp = self.session.get(
                f"{self.clob_base}/book",
                params={"token_id": token_id},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error(f"Failed to fetch orderbook for {token_id}: {e}")
            return {"bids": [], "asks": []}

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price from orderbook."""
        try:
            resp = self.session.get(
                f"{self.clob_base}/midpoint",
                params={"token_id": token_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("mid", 0))
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Failed to get midpoint for {token_id}: {e}")
            return None

    def get_spread(self, token_id: str) -> Optional[dict]:
        """Get bid-ask spread for a token using real prices (not $0.01/$0.99 dust)."""
        book = self.get_orderbook(token_id)
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if not bids or not asks:
            return None

        # Filter out dust orders far from actual price
        real_bids = [float(b["price"]) for b in bids if float(b["price"]) > 0.05]
        real_asks = [float(a["price"]) for a in asks if float(a["price"]) < 0.95]

        if not real_bids or not real_asks:
            # Fall back to raw best bid/ask
            best_bid = float(bids[0].get("price", 0))
            best_ask = float(asks[0].get("price", 0))
        else:
            best_bid = max(real_bids)
            best_ask = min(real_asks)

        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread": spread,
            "spread_pct": spread / best_ask if best_ask > 0 else 0,
        }
