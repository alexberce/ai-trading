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
    """Lightweight HTTP handler for /health and /status endpoints."""

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        elif self.path == "/status":
            stats = {}
            if _health_state["risk_mgr"]:
                stats = _health_state["risk_mgr"].get_stats()
            body = {
                "status": "ok",
                "started_at": _health_state["started_at"],
                "last_scan": _health_state["last_scan"],
                "scan_count": _health_state["scan_count"],
                "portfolio": stats,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body, indent=2).encode())
        else:
            self.send_response(404)
            self.end_headers()

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
    logger.info(f"Bankroll: ${config.BANKROLL:,.2f}")
    logger.info(f"Min edge: {config.MIN_EDGE_THRESHOLD:.0%}")
    logger.info(f"Max Kelly: {config.MAX_KELLY_FRACTION:.0%}")
    logger.info(f"Scan interval: {config.SCAN_INTERVAL_SECONDS}s")
    logger.info(f"LLM estimation: {'enabled' if config.LLM_ESTIMATION_ENABLED else 'disabled'}")

    # Start health check server for Railway
    start_health_server()

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

                # Execute top opportunities
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

        # ── Check pending orders ──────────────────────────────────
        if now - last_order_check >= config.POSITION_CHECK_SECONDS:
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
