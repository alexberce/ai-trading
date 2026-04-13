"""
Main Trading Loop
Orchestrates the full system: scan → estimate → size → execute → monitor.

Run modes:
  python main.py              # Full auto-trading loop
  python main.py --scan       # Scan only (no execution)
  python main.py --dashboard  # Export dashboard data only
"""
import sys
import time
import json
import signal
import logging
import argparse
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
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


# ─── Health Check Server ─────────────────────────────────────────────
_health_state = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_scan": None,
    "scan_count": 0,
    "risk_mgr": None,
}


class HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for dashboard, health check, and API endpoints."""

    def do_GET(self):
        if self.path == "/":
            self._serve_dashboard()
        elif self.path == "/health":
            self._json_response({"status": "ok"})
        elif self.path == "/status":
            stats = {}
            if _health_state["risk_mgr"]:
                stats = _health_state["risk_mgr"].get_stats()
            self._json_response({
                "status": "ok",
                "started_at": _health_state["started_at"],
                "last_scan": _health_state["last_scan"],
                "scan_count": _health_state["scan_count"],
                "portfolio": stats,
            })
        elif self.path == "/api/dashboard":
            self._serve_dashboard_data()
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_dashboard(self):
        try:
            import os
            html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
            with open(html_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._json_response({"status": "ok", "message": "Dashboard HTML not found"})

    def _serve_dashboard_data(self):
        try:
            with open(config.DASHBOARD_DATA_FILE, "r") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(content.encode())
        except FileNotFoundError:
            self._json_response({
                "updated_at": None, "last_scan": None,
                "portfolio": {
                    "bankroll": 0, "initial_bankroll": 0, "total_return": 0,
                    "total_pnl_closed": 0, "peak_bankroll": 0, "drawdown": 0,
                    "open_positions": 0, "total_exposure": 0, "exposure_pct": 0,
                    "closed_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                    "avg_win": 0, "avg_loss": 0, "is_halted": False, "halt_reason": "",
                },
                "open_positions": [], "closed_positions": [], "opportunities": [],
            })

    def _json_response(self, data):
        body = json.dumps(data, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # Suppress default request logging


def start_health_server():
    """Start the health check HTTP server in a background daemon thread."""
    if not config.HEALTH_CHECK_ENABLED:
        return
    server = HTTPServer(("0.0.0.0", config.PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health check server started on port {config.PORT}")


def export_dashboard_data(
    risk_mgr: RiskManager,
    opportunities: list,
    last_scan_time: str,
):
    """Export current state as JSON for the dashboard."""
    data = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_scan": last_scan_time,
        "portfolio": risk_mgr.get_stats(),
        "open_positions": risk_mgr.open_positions,
        "closed_positions": risk_mgr.closed_positions[-20:],  # Last 20
        "opportunities": [o.to_dict() for o in opportunities[:15]],
    }
    with open(config.DASHBOARD_DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def run_scan_only():
    """Run a single scan and print opportunities (no trading)."""
    logger.info("=== SCAN-ONLY MODE ===")

    fetcher = MarketFetcher()
    estimator = ProbabilityEstimator()
    risk_mgr = RiskManager()
    finder = EdgeFinder(fetcher, estimator, risk_mgr)

    opportunities = finder.scan()

    print("\n" + "=" * 80)
    print(f"  PREDICTION MARKET SCAN - {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 80)

    if not opportunities:
        print("\n  No opportunities found meeting edge threshold.")
    else:
        print(f"\n  Found {len(opportunities)} opportunities:\n")
        for i, opp in enumerate(opportunities[:10]):
            d = opp.to_dict()
            print(f"  #{i+1} [{d['direction'].upper()}] {d['question'][:60]}")
            print(f"      Market: {d['market_price']:.0%} → Estimate: {d['estimated_prob']:.0%} "
                  f"(Edge: {d['edge']:+.1%})")
            print(f"      Confidence: {d['confidence']:.0%} | "
                  f"Score: {d['score']:.1f} | "
                  f"Size: ${d['sizing'].get('total_cost', 0):.0f}")
            print(f"      {d['reasoning'][:100]}")
            print()

    print("=" * 80)
    stats = risk_mgr.get_stats()
    print(f"  Bankroll: ${stats['bankroll']:,.2f} | "
          f"Exposure: {stats['exposure_pct']:.0%} | "
          f"Open: {stats['open_positions']}")
    print("=" * 80)

    export_dashboard_data(risk_mgr, opportunities, datetime.now(timezone.utc).isoformat())
    return opportunities


def run_trading_loop():
    """Main auto-trading loop."""
    logger.info("=== STARTING TRADING SYSTEM ===")
    logger.info(f"Min edge: {config.MIN_EDGE_THRESHOLD:.0%}")
    logger.info(f"Max Kelly: {config.MAX_KELLY_FRACTION:.0%}")
    logger.info(f"Scan interval: {config.SCAN_INTERVAL_SECONDS}s")
    logger.info(f"LLM estimation: {'enabled' if config.LLM_ESTIMATION_ENABLED else 'disabled'}")

    # Start health check server for Railway
    start_health_server()

    # Acquire leader lock — only one instance trades at a time
    is_leader = False
    if config.DATABASE_URL:
        is_leader = db.try_acquire_leader_lock()
    else:
        is_leader = True  # No DB = single instance assumed

    if not is_leader:
        logger.info("Running in SCAN-ONLY mode (another instance is the leader)")

    # Initialize components
    fetcher = MarketFetcher()
    estimator = ProbabilityEstimator()
    risk_mgr = RiskManager()
    executor = Executor()
    finder = EdgeFinder(fetcher, estimator, risk_mgr)

    # Wire up health state
    _health_state["risk_mgr"] = risk_mgr

    # Load saved state if exists
    risk_mgr.load_state()

    # Fetch bankroll from Polymarket
    balance = executor.get_balance()
    if balance is None:
        logger.error("Cannot fetch balance from Polymarket. Check API credentials. "
                     "Dashboard will stay up but trading is disabled.")
        is_leader = False  # Disable trading if we can't get balance
    else:
        risk_mgr.sync_bankroll(balance)
        logger.info(f"Bankroll from Polymarket: ${balance:,.2f}")

    last_scan = 0
    last_order_check = 0
    last_rebalance = 0
    opportunities = []

    while not _shutdown:
        now = time.time()

        # ── Periodic market scan ──────────────────────────────────
        if now - last_scan >= config.SCAN_INTERVAL_SECONDS:
            logger.info("Running market scan...")
            try:
                opportunities = finder.scan()

                # Execute top opportunities (leader only)
                if is_leader:
                    for opp in opportunities[:3]:  # Max 3 new trades per cycle
                        if not finder.should_trade(opp):
                            continue

                        token_id = (
                            opp.market.yes_token_id
                            if opp.estimate.direction == "yes"
                            else opp.market.no_token_id
                        )
                        target_price = opp.sizing["cost_per_share"]

                        order = executor.execute_trade(
                            token_id=token_id,
                            direction=opp.estimate.direction,
                            num_shares=opp.sizing["num_shares"],
                            target_price=target_price,
                        )

                        if order:
                            risk_mgr.open_position(opp.estimate, opp.sizing, order.id)
                            logger.info(f"Trade executed: {opp}")

                scan_time = datetime.now(timezone.utc).isoformat()
                export_dashboard_data(risk_mgr, opportunities, scan_time)
                _health_state["last_scan"] = scan_time
                _health_state["scan_count"] += 1
                last_scan = now

            except Exception as e:
                logger.error(f"Scan cycle error: {e}", exc_info=True)

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

        # Brief sleep to avoid busy-waiting
        time.sleep(5)

    # ── Graceful shutdown ─────────────────────────────────────────
    logger.info("Shutting down...")
    risk_mgr.save_state()
    export_dashboard_data(risk_mgr, opportunities, datetime.now(timezone.utc).isoformat())
    if is_leader and config.DATABASE_URL:
        db.release_leader_lock()
    logger.info("Shutdown complete.")


def main():
    parser = argparse.ArgumentParser(description="Prediction Market Trading System")
    parser.add_argument("--scan", action="store_true", help="Scan only, no execution")
    parser.add_argument("--dashboard", action="store_true", help="Export dashboard data only")
    args = parser.parse_args()

    if args.scan:
        run_scan_only()
    elif args.dashboard:
        risk_mgr = RiskManager()
        risk_mgr.load_state()
        export_dashboard_data(risk_mgr, [], datetime.now(timezone.utc).isoformat())
        print(f"Dashboard data exported to {config.DASHBOARD_DATA_FILE}")
    else:
        run_trading_loop()


if __name__ == "__main__":
    main()
