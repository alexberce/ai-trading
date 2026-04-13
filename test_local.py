"""Quick local smoke test — verifies API connectivity and market parsing."""
import sys
import os
os.environ.setdefault("LLM_ESTIMATION_ENABLED", "false")  # Skip LLM calls for testing

from market_fetcher import MarketFetcher
from executor import Executor
from probability_estimator import ProbabilityEstimator
from risk_manager import RiskManager
from edge_finder import EdgeFinder
import config

def test_balance():
    print("=== Balance Test ===")
    ex = Executor()
    bal = ex.get_balance()
    print(f"Balance: ${bal:.2f}" if bal is not None else "Balance: FAILED")
    assert bal is not None, "Balance fetch failed"
    return bal

def test_markets():
    print("\n=== Market Fetch Test ===")
    fetcher = MarketFetcher()
    markets = fetcher.fetch_active_markets(limit=20)
    print(f"Fetched {len(markets)} markets passing filters")
    for m in markets[:5]:
        print(f"  {m.question[:60]}")
        print(f"    YES={m.yes_price:.2f} NO={m.no_price:.2f} liq=${m.liquidity:,.0f} vol={m.volume:,.0f}")
        print(f"    token_id={m.yes_token_id[:20]}...")
    assert len(markets) > 0, "No markets found"
    return markets

def test_estimator(markets):
    print("\n=== Estimator Test ===")
    estimator = ProbabilityEstimator()
    for m in markets[:3]:
        est = estimator.estimate(m)
        print(f"  {m.question[:50]}")
        print(f"    Market={est.market_prob:.0%} Est={est.estimated_prob:.0%} Edge={est.edge:+.1%} Conf={est.confidence:.0%}")

def test_scan(markets):
    print("\n=== Full Scan Test ===")
    risk_mgr = RiskManager(bankroll=200)
    estimator = ProbabilityEstimator()
    fetcher = MarketFetcher()
    finder = EdgeFinder(fetcher, estimator, risk_mgr)
    opps = finder.scan(max_markets=20)
    print(f"Found {len(opps)} opportunities")
    for o in opps[:3]:
        print(f"  {o}")

if __name__ == "__main__":
    try:
        bal = test_balance()
        markets = test_markets()
        test_estimator(markets)
        test_scan(markets)
        print("\n✓ All tests passed")
    except Exception as e:
        print(f"\n✗ FAILED: {e}")
        sys.exit(1)
