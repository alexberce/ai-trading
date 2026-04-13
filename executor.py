"""
Executor
Handles order placement, monitoring, and cancellation via Polymarket CLOB API.

Note: Polymarket uses an on-chain settlement system with off-chain orderbook (CLOB).
Orders are signed with your Ethereum wallet and submitted to the CLOB.
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


class Order:
    """Represents a placed order."""

    def __init__(self, raw: dict):
        self.id = raw.get("id", "")
        self.status = raw.get("status", "unknown")
        self.token_id = raw.get("asset_id", "")
        self.side = raw.get("side", "")  # BUY or SELL
        self.price = float(raw.get("price", 0))
        self.size = float(raw.get("original_size", 0))
        self.filled = float(raw.get("size_matched", 0))
        self.remaining = self.size - self.filled
        self.created_at = raw.get("created_at", "")
        self.raw = raw

    @property
    def is_filled(self) -> bool:
        return self.status == "MATCHED" or self.remaining <= 0

    @property
    def is_open(self) -> bool:
        return self.status in ("LIVE", "OPEN")

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
    """
    Executes trades on Polymarket's CLOB.

    Supports:
    - Limit order placement
    - Order status monitoring
    - Order cancellation
    - Fill tracking
    """

    def __init__(self):
        self.base_url = config.POLYMARKET_API_BASE
        self.api_key = config.POLY_API_KEY
        self.api_secret = config.POLY_API_SECRET
        self.api_passphrase = config.POLY_API_PASSPHRASE
        self.session = requests.Session()
        self.pending_orders: list[Order] = []
        self.filled_orders: list[Order] = []

    def _get_headers(self, method: str, path: str, body: str = "") -> dict:
        """Generate L2 authentication headers for CLOB API."""
        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + path
        if body:
            message += body.replace("'", '"')

        # Secret is base64url-encoded — decode it before HMAC
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

    def place_limit_order(
        self,
        token_id: str,
        side: str,  # "BUY" or "SELL"
        price: float,
        size: int,
        expiration: Optional[int] = None,
    ) -> Optional[Order]:
        """
        Place a limit order on Polymarket CLOB.

        Args:
            token_id: The token to trade (YES or NO token ID)
            side: "BUY" or "SELL"
            price: Limit price (0-1 for prediction markets)
            size: Number of shares
            expiration: Unix timestamp for order expiration (optional)
        """
        if not self.api_key:
            logger.error("No API key configured - cannot place orders")
            return None

        path = "/order"
        order_payload = {
            "tokenID": token_id,
            "price": round(price, 2),
            "size": size,
            "side": side,
            "feeRateBps": 0,  # Polymarket handles fees
            "nonce": str(int(time.time() * 1000)),
            "expiration": expiration or 0,
        }

        body = json.dumps(order_payload)
        headers = self._get_headers("POST", path, body)

        try:
            resp = self.session.post(
                f"{self.base_url}{path}",
                headers=headers,
                data=body,
                timeout=15,
            )

            if resp.status_code == 200:
                data = resp.json()
                order = Order(data)
                self.pending_orders.append(order)
                logger.info(
                    f"Order placed: {side} {size} shares @ ${price:.2f} "
                    f"(ID: {order.id})"
                )
                return order
            else:
                logger.error(f"Order failed: {resp.status_code} - {resp.text}")
                return None

        except requests.RequestException as e:
            logger.error(f"Order placement error: {e}")
            return None

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
        """
        Check all pending orders and update their status.
        Cancel orders that have exceeded timeout.
        """
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
                # Check if timed out
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
        """
        High-level trade execution.
        Places a limit order slightly better than the target price.
        """
        side = "BUY"

        # Place limit order slightly inside the spread
        if direction == "yes":
            limit_price = min(target_price + config.LIMIT_OFFSET, 0.99)
        else:
            limit_price = min(target_price + config.LIMIT_OFFSET, 0.99)

        order = self.place_limit_order(
            token_id=token_id,
            side=side,
            price=round(limit_price, 2),
            size=num_shares,
        )

        return order

    def get_open_orders_summary(self) -> list[dict]:
        """Get summary of all pending orders."""
        return [o.to_dict() for o in self.pending_orders]

    def get_fills_summary(self) -> list[dict]:
        """Get summary of all filled orders."""
        return [o.to_dict() for o in self.filled_orders]

    def get_balance(self) -> Optional[float]:
        """
        Fetch portfolio value from Polymarket Data API.
        Uses the public /value endpoint (no auth needed) with the proxy wallet address.
        Falls back to CLOB balance-allowance if Data API fails.
        """
        proxy_wallet = config.PROXY_WALLET_ADDRESS
        if proxy_wallet:
            try:
                resp = self.session.get(
                    f"https://data-api.polymarket.com/value",
                    params={"user": proxy_wallet},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data and isinstance(data, list) and len(data) > 0:
                        value = float(data[0].get("value", 0))
                        logger.info(f"Polymarket portfolio value: ${value:.2f}")
                        return value
            except Exception as e:
                logger.warning(f"Data API balance fetch failed: {e}")

        # Fallback: CLOB balance-allowance
        if not self.api_key:
            logger.error("No API key configured — cannot fetch balance")
            return None

        path = "/balance-allowance"
        headers = self._get_headers("GET", path)
        try:
            resp = self.session.get(
                f"{self.base_url}{path}",
                headers=headers,
                params={"asset_type": "COLLATERAL"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                balance = float(data.get("balance", "0"))
                if balance > 1_000_000:
                    balance = balance / 1_000_000
                logger.info(f"CLOB balance: ${balance:.2f}")
                return balance
        except Exception as e:
            logger.error(f"CLOB balance fetch error: {e}")

        return None
