"""
Main Trading Loop
Orchestrates: scan → estimate → scalp → monitor.

Real-time architecture:
- SSE push to dashboard (no polling, no JSON files)
- All scan results persisted in PostgreSQL
- Scalper runs every 30s for short-term trades
- LLM scanner runs every 5min for edge detection
"""
import os
import sys
import time
import json
import signal
import logging
import argparse
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

from market_fetcher import MarketFetcher
from probability_estimator import ProbabilityEstimator
from risk_manager import RiskManager
from edge_finder import EdgeFinder
from executor import Executor
import config
import db

# ─── Logging Setup ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE),
    ],
)
logger = logging.getLogger("main")

# Graceful shutdown
_shutdown = False

def _signal_handler(sig, frame):
    global _shutdown
    logger.info("Shutdown signal received, finishing current cycle...")
    _shutdown = True

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ─── SSE (Server-Sent Events) ───────────────────────────────────────
_sse_clients: list = []
_sse_lock = threading.Lock()


def broadcast_sse(event: str, data: dict):
    """Send an SSE event to all connected dashboard clients."""
    payload = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()
    with _sse_lock:
        dead = []
        for wfile in _sse_clients:
            try:
                wfile.write(payload)
                wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                dead.append(wfile)
        for d in dead:
            _sse_clients.remove(d)


# ─── Shared State ────────────────────────────────────────────────────
_state = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_scan": None,
    "scan_count": 0,
    "risk_mgr": None,
    "executor": None,
}


def build_dashboard_payload() -> dict:
    """Assemble dashboard data from DB + in-memory state."""
    risk_mgr = _state.get("risk_mgr")
    executor = _state.get("executor")

    portfolio = risk_mgr.get_stats() if risk_mgr else {}

    # Merge bot positions with live Polymarket positions
    positions = list(risk_mgr.open_positions) if risk_mgr else []
    if executor:
        try:
            live = executor.get_positions()
            bot_tokens = {p.get("token_id") for p in positions}
            for lp in live:
                if lp.get("token_id") not in bot_tokens:
                    positions.append(lp)
        except Exception:
            pass

    # Read persisted data from DB
    opportunities = []
    estimates = []
    scan_progress = None
    latest_scan = None
    closed = []
    if config.DATABASE_URL:
        try:
            opportunities = db.get_latest_opportunities()
            estimates = db.get_latest_estimates()
            scan_progress = db.get_scan_progress()
            latest_scan = db.get_latest_scan()
            closed = db.get_closed_trades(20)
        except Exception as e:
            logger.warning(f"DB read for dashboard failed: {e}")

    # All markets from DB (persisted across deploys)
    all_markets = []
    if config.DATABASE_URL:
        try:
            all_markets = db.get_all_markets()
        except Exception:
            pass
    if not all_markets:
        all_markets = _state.get("all_markets", [])

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_scan": latest_scan["scanned_at"].isoformat() if latest_scan and latest_scan.get("scanned_at") else _state.get("last_scan"),
        "portfolio": portfolio,
        "open_positions": positions,
        "closed_positions": closed or (risk_mgr.closed_positions[-20:] if risk_mgr else []),
        "opportunities": opportunities,
        "scan_progress": scan_progress,
        "scanned_markets": estimates,
        "all_markets": all_markets,
        "banned_markets": db.get_banned_markets_list() if config.DATABASE_URL else [],
    }


# ─── HTTP Server ─────────────────────────────────────────────────────

class AppHandler(BaseHTTPRequestHandler):
    """HTTP handler for dashboard, SSE, health check, and API."""

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_len)) if content_len > 0 else {}

        if self.path == "/api/ban":
            market_id = body.get("market_id", "")
            question = body.get("question", "")
            if market_id and config.DATABASE_URL:
                db.ban_market(market_id, question)
                self._json_response({"ok": True, "action": "banned", "market_id": market_id})
            else:
                self._json_response({"ok": False, "error": "market_id required"})

        elif self.path == "/api/unban":
            market_id = body.get("market_id", "")
            if market_id and config.DATABASE_URL:
                db.unban_market(market_id)
                self._json_response({"ok": True, "action": "unbanned", "market_id": market_id})
            else:
                self._json_response({"ok": False, "error": "market_id required"})

        elif self.path == "/api/close":
            token_id = body.get("token_id", "")
            size = int(body.get("size", 0))
            price = float(body.get("price", 0))
            if token_id and size > 0 and price > 0:
                executor = _state.get("executor")
                if executor:
                    order = executor.place_order(token_id, "SELL", price, size)
                    if order:
                        self._json_response({"ok": True, "action": "sell_order_placed", "order_id": order.id})
                    else:
                        self._json_response({"ok": False, "error": "order failed"})
                else:
                    self._json_response({"ok": False, "error": "executor not ready"})
            else:
                self._json_response({"ok": False, "error": "token_id, size, price required"})
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == "/":
            self._serve_dashboard()
        elif self.path == "/health":
            self._json_response({"status": "ok"})
        elif self.path == "/status":
            self._json_response({
                "status": "ok",
                "started_at": _state["started_at"],
                "last_scan": _state["last_scan"],
                "scan_count": _state["scan_count"],
            })
        elif self.path == "/api/dashboard":
            self._json_response(build_dashboard_payload())
        elif self.path == "/api/events":
            self._serve_sse()
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_dashboard(self):
        try:
            html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
            with open(html_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._json_response({"error": "Dashboard HTML not found"})

    def _serve_sse(self):
        """SSE endpoint — keeps connection open, pushes events."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        with _sse_lock:
            _sse_clients.append(self.wfile)

        # Send current state immediately
        try:
            payload = build_dashboard_payload()
            msg = f"event: dashboard\ndata: {json.dumps(payload, default=str)}\n\n".encode()
            self.wfile.write(msg)
            self.wfile.flush()
        except Exception:
            pass

        # Keep alive until client disconnects
        try:
            while not _shutdown:
                time.sleep(15)
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            with _sse_lock:
                if self.wfile in _sse_clients:
                    _sse_clients.remove(self.wfile)

    def _json_response(self, data):
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def start_server():
    """Start the HTTP server in a background daemon thread."""
    if not config.HEALTH_CHECK_ENABLED:
        return
    server = ThreadingHTTPServer(("0.0.0.0", config.PORT), AppHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Server started on port {config.PORT}")


# ─── Trading Loop ────────────────────────────────────────────────────

def run_trading_loop():
    """Main auto-trading loop with scalper + LLM scanner."""
    logger.info("=== STARTING TRADING SYSTEM ===")
    logger.info(f"Min edge: {config.MIN_EDGE_THRESHOLD:.0%}")
    logger.info(f"Scan interval: {config.SCAN_INTERVAL_SECONDS}s")
    logger.info(f"Scalp interval: {config.SCALP_SCAN_INTERVAL}s")
    logger.info(f"LLM estimation: {'enabled' if config.LLM_ESTIMATION_ENABLED else 'disabled'}")
    logger.info(f"Scalping: {'enabled' if config.SCALP_ENABLED else 'disabled'}")

    # Start HTTP server
    start_server()

    # Leader election — force acquire (kill stale locks from crashed deploys)
    is_leader = True
    if config.DATABASE_URL:
        try:
            conn = db.get_connection()
            with conn.cursor() as cur:
                # Terminate any other backend holding our lock
                cur.execute("""
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE pid != pg_backend_pid()
                    AND query LIKE '%advisory%'
                """)
                cur.execute("SELECT pg_advisory_lock(%s)", (db.LEADER_LOCK_ID,))
            logger.info("Acquired leader lock (forced)")
        except Exception as e:
            logger.warning(f"Leader lock failed: {e}, proceeding as leader anyway")
            is_leader = True

    # Initialize components
    fetcher = MarketFetcher()
    estimator = ProbabilityEstimator()
    risk_mgr = RiskManager()
    executor = Executor()
    finder = EdgeFinder(fetcher, estimator, risk_mgr)

    _state["risk_mgr"] = risk_mgr
    _state["executor"] = executor

    # Load saved state
    risk_mgr.load_state()

    # Sync bankroll from Polymarket
    balance = executor.get_balance()
    if balance is None:
        logger.warning("Cannot fetch balance from Polymarket. Trading disabled.")
        risk_mgr.current_bankroll = 0
        risk_mgr.initial_bankroll = 0
        risk_mgr.peak_bankroll = 0
        is_leader = False
    else:
        risk_mgr.sync_bankroll(balance)
        logger.info(f"Bankroll from Polymarket: ${balance:,.2f}")

    # Scan immediately, then every SCAN_INTERVAL_SECONDS for LLM analysis
    last_scan = 0

    last_scalp_check = 0
    last_order_check = 0

    # Import scalper (lazy — only if enabled)
    scalper = None
    if config.SCALP_ENABLED:
        try:
            from scalper import Scalper
            scalper = Scalper(fetcher, executor, risk_mgr)
            logger.info("Scalper initialized")
        except ImportError:
            logger.warning("scalper.py not found, scalping disabled")

    # Fetch all markets for dashboard display
    last_market_refresh = 0

    def refresh_all_markets():
        """Fetch markets and persist to DB."""
        nonlocal last_market_refresh
        try:
            markets = fetcher.fetch_active_markets(limit=500)
            market_dicts = [
                {
                    "market_id": m.id,
                    "condition_id": m.condition_id,
                    "question": m.question,
                    "category": m.category,
                    "yes_price": m.yes_price,
                    "no_price": m.no_price,
                    "liquidity": m.liquidity,
                    "volume": m.volume,
                    "end_date": m.end_date,
                }
                for m in markets
            ]
            if config.DATABASE_URL:
                db.save_markets(market_dicts)
            _state["all_markets"] = market_dicts
            last_market_refresh = time.time()
            logger.info(f"Refreshed {len(markets)} markets")
        except Exception as e:
            logger.error(f"Market refresh error: {e}")

    refresh_all_markets()

    # Broadcast initial state
    broadcast_sse("dashboard", build_dashboard_payload())

    while not _shutdown:
        now = time.time()

        # ── Scalper cycle (every 30s) ────────────────────────────
        if scalper and is_leader and now - last_scalp_check >= config.SCALP_SCAN_INTERVAL:
            try:
                actions = scalper.tick()
                if actions:
                    broadcast_sse("dashboard", build_dashboard_payload())
                last_scalp_check = now
            except Exception as e:
                logger.error(f"Scalper error: {e}", exc_info=True)

        # ── LLM market scan (every 5min) ─────────────────────────
        if now - last_scan >= config.SCAN_INTERVAL_SECONDS:
            logger.info("Running market scan...")
            scan_start = time.time()
            try:
                all_estimates = []

                def _on_progress(done, total, with_edge, all_est):
                    all_estimates.clear()
                    all_estimates.extend(all_est)
                    if config.DATABASE_URL:
                        db.save_scan_progress(done, total)
                    broadcast_sse("scan_progress", {
                        "done": done, "total": total,
                        "scanned_markets": [e.to_dict() for e in all_est],
                    })
                    logger.info(f"Scan: {done}/{total} markets, {len(with_edge)} with edge")

                opportunities = finder.scan(on_progress=_on_progress)
                scan_duration = time.time() - scan_start

                # Persist scan results to DB
                if config.DATABASE_URL:
                    scan_id = db.save_scan_results(
                        markets_scanned=len(fetcher._cache.get("markets", [])),
                        estimates_with_edge=len(all_estimates),
                        opportunities_count=len(opportunities),
                        duration=scan_duration,
                    )
                    db.save_estimates(scan_id, [e.to_dict() for e in all_estimates])  # ALL markets, not just edge
                    db.save_opportunities(scan_id, [o.to_dict() for o in opportunities])
                    db.save_scan_progress(0, 0)  # Clear progress (hides bar)

                # Execute top opportunities (leader only)
                if is_leader:
                    # Get all existing positions to avoid duplicates
                    existing = list(risk_mgr.open_positions)
                    try:
                        existing += executor.get_positions()
                    except Exception:
                        pass

                    for opp in opportunities[:3]:
                        if not finder.should_trade(opp, existing):
                            continue
                        token_id = (
                            opp.market.yes_token_id
                            if opp.estimate.direction == "yes"
                            else opp.market.no_token_id
                        )
                        order = executor.execute_trade(
                            token_id=token_id,
                            direction=opp.estimate.direction,
                            num_shares=opp.sizing["num_shares"],
                            target_price=opp.sizing["cost_per_share"],
                        )
                        if order:
                            risk_mgr.open_position(opp.estimate, opp.sizing, order.id)
                            broadcast_sse("trade_executed", {"question": opp.market.question})
                            logger.info(f"Trade executed: {opp}")

                _state["last_scan"] = datetime.now(timezone.utc).isoformat()
                _state["scan_count"] += 1
                last_scan = now

                broadcast_sse("scan_complete", {
                    "opportunities": len(opportunities),
                    "estimates": len(all_estimates),
                    "duration": round(scan_duration, 1),
                })
                broadcast_sse("dashboard", build_dashboard_payload())

            except Exception as e:
                logger.error(f"Scan cycle error: {e}", exc_info=True)

        # ── Refresh market list for dashboard (every 60s) ─────────
        if now - last_market_refresh >= 60:
            refresh_all_markets()
            broadcast_sse("dashboard", build_dashboard_payload())

        # ── Check pending orders (leader only) ────────────────────
        if is_leader and now - last_order_check >= config.POSITION_CHECK_SECONDS:
            try:
                executor.monitor_pending_orders()
                last_order_check = now
            except Exception as e:
                logger.error(f"Order check error: {e}")

        # ── Save state periodically ───────────────────────────────
        try:
            risk_mgr.save_state()
        except Exception as e:
            logger.error(f"State save error: {e}")

        time.sleep(1)

    # ── Graceful shutdown ─────────────────────────────────────────
    logger.info("Shutting down...")
    risk_mgr.save_state()
    if is_leader and config.DATABASE_URL:
        db.release_leader_lock()
    logger.info("Shutdown complete.")


def run_scan_only():
    """Run a single scan and print opportunities."""
    logger.info("=== SCAN-ONLY MODE ===")
    fetcher = MarketFetcher()
    estimator = ProbabilityEstimator()
    risk_mgr = RiskManager()
    finder = EdgeFinder(fetcher, estimator, risk_mgr)

    opportunities = finder.scan()

    print(f"\nFound {len(opportunities)} opportunities:\n")
    for i, opp in enumerate(opportunities[:10]):
        d = opp.to_dict()
        print(f"  #{i+1} [{d['direction'].upper()}] {d['question'][:60]}")
        print(f"      Market: {d['market_price']:.0%} → Estimate: {d['estimated_prob']:.0%} "
              f"(Edge: {d['edge']:+.1%})")
        print()


def main():
    parser = argparse.ArgumentParser(description="Prediction Market Trading System")
    parser.add_argument("--scan", action="store_true", help="Scan only, no execution")
    args = parser.parse_args()

    if args.scan:
        run_scan_only()
    else:
        run_trading_loop()


if __name__ == "__main__":
    main()
