"""
WebSocket feeds for real-time data.

Two connections:
1. Sports WS — live scores, game status changes
2. Market WS — live price updates for subscribed tokens

Both run in background threads and update shared state.
"""
import json
import time
import threading
import logging
from typing import Optional
import websocket

import config

logger = logging.getLogger(__name__)


class SportsFeed:
    """Real-time sports scores and game state via WebSocket."""

    def __init__(self):
        self.games: dict[str, dict] = {}  # slug -> game state
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self):
        """Start the sports WebSocket in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Sports WebSocket feed started")

    def stop(self):
        self._running = False
        if self._ws:
            self._ws.close()

    def _run(self):
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    "wss://sports-api.polymarket.com/ws",
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_ping=self._on_ping,
                )
                self._ws.run_forever(ping_interval=10, ping_timeout=5)
            except Exception as e:
                logger.error(f"Sports WS error: {e}")
            if self._running:
                logger.info("Sports WS reconnecting in 5s...")
                time.sleep(5)

    def _on_message(self, ws, message):
        if message == "ping":
            ws.send("pong")
            return

        try:
            data = json.loads(message)
            slug = data.get("slug", "")
            if slug:
                self.games[slug] = data
                if data.get("live"):
                    logger.debug(f"Live: {slug} score={data.get('score')} period={data.get('period')}")
        except json.JSONDecodeError:
            pass

    def _on_error(self, ws, error):
        logger.warning(f"Sports WS error: {error}")

    def _on_close(self, ws, close_code, close_msg):
        logger.info(f"Sports WS closed: {close_code}")

    def _on_ping(self, ws, data):
        ws.send("pong")

    def get_live_games(self) -> list[dict]:
        """Get all currently live games."""
        return [g for g in self.games.values() if g.get("live") and not g.get("ended")]

    def is_game_live(self, slug: str) -> bool:
        """Check if a specific game is currently live."""
        game = self.games.get(slug, {})
        return game.get("live", False) and not game.get("ended", False)


class MarketFeed:
    """Real-time price updates for subscribed tokens via WebSocket."""

    def __init__(self):
        self.prices: dict[str, float] = {}  # token_id -> latest price
        self.best_bid: dict[str, float] = {}  # token_id -> best bid
        self.best_ask: dict[str, float] = {}  # token_id -> best ask
        self._subscribed_tokens: list[str] = []
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._on_price_change: Optional[callable] = None

    def start(self, on_price_change=None):
        """Start the market WebSocket in a background thread."""
        self._running = True
        self._on_price_change = on_price_change
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Market WebSocket feed started")

    def stop(self):
        self._running = False
        if self._ws:
            self._ws.close()

    def subscribe(self, token_ids: list[str]):
        """Subscribe to price updates for these tokens."""
        new_tokens = [t for t in token_ids if t not in self._subscribed_tokens]
        if not new_tokens:
            return

        self._subscribed_tokens.extend(new_tokens)
        if self._ws:
            try:
                msg = json.dumps({
                    "assets_ids": new_tokens,
                    "type": "market",
                    "custom_feature_enabled": True,
                })
                self._ws.send(msg)
                logger.info(f"Subscribed to {len(new_tokens)} tokens (total: {len(self._subscribed_tokens)})")
            except Exception as e:
                logger.warning(f"Subscribe error: {e}")

    def _run(self):
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    "wss://ws-subscriptions-clob.polymarket.com/ws/market",
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                logger.error(f"Market WS error: {e}")
            if self._running:
                logger.info("Market WS reconnecting in 5s...")
                time.sleep(5)

    def _on_open(self, ws):
        # Re-subscribe to all tokens on reconnect
        if self._subscribed_tokens:
            msg = json.dumps({
                "assets_ids": self._subscribed_tokens,
                "type": "market",
                "custom_feature_enabled": True,
            })
            ws.send(msg)
            logger.info(f"Market WS connected, subscribed to {len(self._subscribed_tokens)} tokens")

    def _on_message(self, ws, message):
        if message == "PONG":
            return

        try:
            data = json.loads(message)

            # Price update messages come in various formats
            # Handle trade events
            if isinstance(data, list):
                for item in data:
                    self._process_update(item)
            elif isinstance(data, dict):
                self._process_update(data)

        except json.JSONDecodeError:
            pass

    def _process_update(self, data: dict):
        """Process a single price/trade update."""
        asset_id = data.get("asset_id", "")
        if not asset_id:
            return

        # Update price from trade
        price = data.get("price")
        if price:
            old_price = self.prices.get(asset_id)
            self.prices[asset_id] = float(price)
            if old_price and self._on_price_change:
                change = (float(price) - old_price) / old_price if old_price > 0 else 0
                if abs(change) > 0.001:  # Only report >0.1% changes
                    self._on_price_change(asset_id, float(price), old_price, change)

        # Update best bid/ask
        if "best_bid" in data:
            self.best_bid[asset_id] = float(data["best_bid"])
        if "best_ask" in data:
            self.best_ask[asset_id] = float(data["best_ask"])

    def _on_error(self, ws, error):
        logger.warning(f"Market WS error: {error}")

    def _on_close(self, ws, close_code, close_msg):
        logger.info(f"Market WS closed: {close_code}")

    def get_price(self, token_id: str) -> Optional[float]:
        return self.prices.get(token_id)
