"""
Executor
Handles order placement, monitoring, and cancellation via Polymarket CLOB API.
Uses py-clob-client for EIP-712 order signing.
"""
import time
import json
import hmac
import hashlib
import base64
import logging
from datetime import datetime, timezone
from typing import Optional
import requests
import config

logger = logging.getLogger(__name__)

# Lazy-loaded CLOB client
_clob_client = None


def _get_clob_client():
    """Initialize the py-clob-client with proper credentials."""
    global _clob_client
    if _clob_client is not None:
        return _clob_client

    if not config.PRIVATE_KEY:
        logger.error("PRIVATE_KEY not set — cannot sign orders")
        return None

    try:
        # Set proxy env var before importing — py-clob-client uses httpx which reads HTTPS_PROXY
        import os
        if config.PROXY_URL:
            os.environ["HTTPS_PROXY"] = config.PROXY_URL
            os.environ["HTTP_PROXY"] = config.PROXY_URL

        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        creds = ApiCreds(
            api_key=config.POLY_API_KEY,
            api_secret=config.POLY_API_SECRET,
            api_passphrase=config.POLY_API_PASSPHRASE,
        )

        _clob_client = ClobClient(
            host=config.POLYMARKET_API_BASE,
            chain_id=137,  # Polygon
            key=config.PRIVATE_KEY,
            creds=creds,
            signature_type=2,  # Proxy wallet
            funder=config.PROXY_WALLET_ADDRESS,  # The proxy wallet that holds funds
        )

        logger.info("CLOB client initialized with order signing")
        return _clob_client

    except Exception as e:
        logger.error(f"Failed to initialize CLOB client: {e}")
        return None


class Order:
    """Represents a placed order."""

    def __init__(self, raw: dict):
        self.id = raw.get("orderID", raw.get("id", ""))
        self.status = raw.get("status", "unknown")
        self.token_id = raw.get("asset_id", "")
        self.side = raw.get("side", "")
        self.price = float(raw.get("price", 0))
        self.size = float(raw.get("original_size", raw.get("size", 0)))
        self.filled = float(raw.get("size_matched", 0))
        self.remaining = self.size - self.filled
        self.created_at = raw.get("created_at", "")
        self.raw = raw

    @property
    def is_filled(self) -> bool:
        return self.status == "MATCHED" or self.remaining <= 0

    @property
    def is_open(self) -> bool:
        return self.status in ("LIVE", "OPEN", "live")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "side": self.side,
            "price": self.price,
            "size": self.size,
            "filled": self.filled,
            "remaining": self.remaining,
        }


class Executor:
    """Executes trades on Polymarket's CLOB using py-clob-client for signing."""

    def __init__(self):
        self.base_url = config.POLYMARKET_API_BASE
        self.api_key = config.POLY_API_KEY
        self.api_secret = config.POLY_API_SECRET
        self.api_passphrase = config.POLY_API_PASSPHRASE
        self.session = requests.Session()
        if config.PROXY_URL:
            self.session.proxies = {
                "http": config.PROXY_URL,
                "https": config.PROXY_URL,
            }
            logger.info(f"Using proxy for CLOB API: {config.PROXY_URL.split('@')[-1] if '@' in config.PROXY_URL else config.PROXY_URL}")
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})
        self.pending_orders: list[Order] = []
        self.filled_orders: list[Order] = []

    def _get_headers(self, method: str, path: str, body: str = "") -> dict:
        """Generate L2 authentication headers for CLOB API."""
        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + path
        if body:
            message += body.replace("'", '"')

        secret_bytes = base64.urlsafe_b64decode(self.api_secret)
        h = hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256)
        signature = base64.urlsafe_b64encode(h.digest()).decode("utf-8")

        return {
            "POLY_ADDRESS": config.WALLET_ADDRESS,
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": self.api_key,
            "POLY_PASSPHRASE": self.api_passphrase,
            "Content-Type": "application/json",
        }

    def place_market_order(
        self,
        token_id: str,
        side: str,
        amount: float,
        price: float = 0,
    ) -> Optional[Order]:
        """
        Place a market order (instant fill).
        BUY: amount = dollars to spend. price = max price (slippage protection).
        SELL: amount = shares to sell. price = min price (slippage protection).
        """
        client = _get_clob_client()
        if not client:
            logger.error("CLOB client not available")
            return None

        try:
            from py_clob_client.order_builder.constants import BUY, SELL
            from py_clob_client.clob_types import MarketOrderArgs, OrderType

            order_side = BUY if side.upper() == "BUY" else SELL

            args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=order_side,
                price=price,
                order_type=OrderType.FAK,
            )

            signed = client.create_market_order(args)
            resp = client.post_order(signed, orderType=OrderType.FAK)

            if resp and resp.get("success"):
                order = Order(resp)
                logger.info(f"Market order filled: {side} ${amount:.2f} (ID: {order.id})")
                return order
            else:
                error = resp.get("errorMsg", resp) if resp else "No response"
                logger.error(f"Market order rejected: {error}")
                return None

        except Exception as e:
            logger.error(f"Market order error: {e}")
            return None

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: int,
        order_type: str = "GTC",
    ) -> Optional[Order]:
        """
        Place a signed order via py-clob-client.
        order_type: GTC (limit), FOK (fill-or-kill/market), FAK (fill-and-kill)
        """
        client = _get_clob_client()
        if not client:
            logger.error("CLOB client not available — cannot place order")
            return None

        try:
            from py_clob_client.order_builder.constants import BUY, SELL
            from py_clob_client.clob_types import OrderArgs, OrderType

            order_side = BUY if side.upper() == "BUY" else SELL

            # Map string to OrderType
            ot = OrderType.GTC
            if order_type == "FOK":
                ot = OrderType.FOK
            elif order_type == "FAK":
                ot = OrderType.FAK

            order_args = OrderArgs(
                token_id=token_id,
                price=round(price, 2),
                size=size,
                side=order_side,
            )

            signed_order = client.create_order(order_args)
            resp = client.post_order(signed_order, orderType=ot)

            if resp and resp.get("success"):
                order = Order(resp)
                self.pending_orders.append(order)
                logger.info(
                    f"Order placed: {side} {size} shares @ ${price:.2f} "
                    f"(ID: {order.id})"
                )
                return order
            else:
                error = resp.get("errorMsg", resp) if resp else "No response"
                logger.error(f"Order rejected: {error}")
                return None

        except Exception as e:
            logger.error(f"Order placement error: {e}", exc_info=True)
            return None

    def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: int,
        expiration: Optional[int] = None,
    ) -> Optional[Order]:
        """Backward-compatible wrapper for place_order."""
        return self.place_order(token_id, side, price, size)

    def check_order(self, order_id: str) -> Optional[Order]:
        """Check the status of an order."""
        path = f"/order/{order_id}"
        headers = self._get_headers("GET", path)

        try:
            resp = self.session.get(
                f"{self.base_url}{path}",
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                return Order(resp.json())
            return None
        except requests.RequestException as e:
            logger.error(f"Order check failed: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        client = _get_clob_client()
        if client:
            try:
                client.cancel(order_id)
                logger.info(f"Order cancelled: {order_id}")
                self.pending_orders = [
                    o for o in self.pending_orders if o.id != order_id
                ]
                return True
            except Exception as e:
                logger.error(f"Cancel error: {e}")
                return False

        # Fallback to raw API
        path = f"/order/{order_id}"
        headers = self._get_headers("DELETE", path)
        try:
            resp = self.session.delete(
                f"{self.base_url}{path}",
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info(f"Order cancelled: {order_id}")
                self.pending_orders = [
                    o for o in self.pending_orders if o.id != order_id
                ]
                return True
            else:
                logger.error(f"Cancel failed: {resp.status_code}")
                return False
        except requests.RequestException as e:
            logger.error(f"Cancel error: {e}")
            return False

    def monitor_pending_orders(self, timeout_seconds: Optional[int] = None):
        """Check all pending orders and update their status."""
        timeout = timeout_seconds or config.ORDER_TIMEOUT_SECONDS
        now = time.time()

        for order in list(self.pending_orders):
            updated = self.check_order(order.id)
            if not updated:
                continue

            if updated.is_filled:
                logger.info(f"Order filled: {order.id} ({updated.filled} shares)")
                self.pending_orders.remove(order)
                self.filled_orders.append(updated)

            elif updated.is_open:
                try:
                    created = datetime.fromisoformat(
                        updated.created_at.replace("Z", "+00:00")
                    ).timestamp()
                    if now - created > timeout:
                        logger.info(f"Order timed out, cancelling: {order.id}")
                        self.cancel_order(order.id)
                except (ValueError, TypeError):
                    pass

    def execute_trade(
        self,
        token_id: str,
        direction: str,
        num_shares: int,
        target_price: float,
    ) -> Optional[Order]:
        """High-level trade execution with slight price improvement."""
        # Both YES and NO tokens are bought the same way — price + offset
        limit_price = min(target_price + config.LIMIT_OFFSET, 0.99)

        return self.place_order(
            token_id=token_id,
            side="BUY",
            price=round(limit_price, 2),
            size=num_shares,
        )

    def get_balance(self) -> Optional[float]:
        """Fetch balance from Polymarket. Uses Data API (no proxy needed)."""
        proxy_wallet = config.PROXY_WALLET_ADDRESS
        if not proxy_wallet:
            logger.error("No PROXY_WALLET_ADDRESS configured")
            return None

        # Get position value from Data API (public, no proxy)
        positions_value = 0.0
        try:
            resp = requests.get(
                "https://data-api.polymarket.com/value",
                params={"user": proxy_wallet},
                headers={"User-Agent": "Mozilla/5.0"},
                proxies={"http": None, "https": None},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, list) and len(data) > 0:
                    positions_value = float(data[0].get("value", 0))
        except Exception as e:
            logger.warning(f"Data API position value fetch failed: {e}")

        # Get cash from CLOB (needs proxy for geoblock)
        cash = 0.0
        try:
            path = "/balance-allowance"
            headers = self._get_headers("GET", path)
            resp = self.session.get(
                f"{self.base_url}{path}",
                headers=headers,
                params={"asset_type": "COLLATERAL", "signature_type": "2"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                raw = float(data.get("balance", "0"))
                # USDC has 6 decimals — always convert
                cash = raw / 1_000_000
        except Exception as e:
            logger.warning(f"CLOB balance fetch failed (proxy issue?): {e}")

        # If CLOB call failed (not just returned 0), use cached balance
        clob_failed = cash == 0  # Will be refined below
        if clob_failed and config.DATABASE_URL:
            try:
                import db
                prev = db.get_balance()
                if prev and prev.get("cash", 0) > 0:
                    cash = prev["cash"]
                    logger.info(f"CLOB failed, using cached cash: ${cash:.2f}")
            except Exception:
                pass

        total = cash + positions_value
        if total == 0:
            logger.warning("Balance returned 0 — proxy may be down")
            return None

        logger.info(f"Balance: ${cash:.2f} cash + ${positions_value:.2f} positions = ${total:.2f}")
        return total

    def get_positions(self) -> list[dict]:
        """Fetch all open positions from Polymarket Data API."""
        proxy_wallet = config.PROXY_WALLET_ADDRESS
        if not proxy_wallet:
            return []

        try:
            # Use a plain session with NO proxy — Data API is public
            resp = requests.get(
                "https://data-api.polymarket.com/positions",
                params={"user": proxy_wallet, "sizeThreshold": "0"},
                headers={"User-Agent": "Mozilla/5.0"},
                proxies={"http": None, "https": None},
                timeout=10,
            )
            if resp.status_code == 200:
                positions = resp.json()
                return [
                    {
                        "question": p.get("title", ""),
                        "direction": p.get("outcome", "").lower(),
                        "category": p.get("eventSlug", ""),
                        "num_shares": p.get("size", 0),
                        "entry_price": p.get("avgPrice", 0),
                        "total_cost": p.get("initialValue", 0),
                        "current_value": p.get("currentValue", 0),
                        "pnl": p.get("cashPnl", 0),
                        "return_pct": (p.get("percentPnl", 0) or 0) / 100,
                        "cur_price": p.get("curPrice", 0),
                        "market_id": p.get("conditionId", ""),
                        "token_id": p.get("asset", ""),
                        "end_date": p.get("endDate", ""),
                        "icon": p.get("icon", ""),
                        "opened_at": None,
                        "edge_at_entry": 0,
                        "source": "polymarket",
                    }
                    for p in positions
                ]
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")

        return []

    def get_open_orders_summary(self) -> list[dict]:
        return [o.to_dict() for o in self.pending_orders]

    def get_fills_summary(self) -> list[dict]:
        return [o.to_dict() for o in self.filled_orders]
