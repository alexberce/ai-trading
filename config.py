"""
Configuration for the prediction market trading system.
All tunable parameters in one place.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ─── API Configuration ───────────────────────────────────────────────
POLYMARKET_API_BASE = "https://clob.polymarket.com"
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"

# Your Polymarket CLOB API credentials
POLY_API_KEY = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE", "")

# Polymarket wallet addresses
# WALLET_ADDRESS = baseAddress from CLOB creds (used for API auth)
# PROXY_WALLET_ADDRESS = the proxy wallet shown in Polymarket UI (used for balance/positions)
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
PROXY_WALLET_ADDRESS = os.getenv("PROXY_WALLET_ADDRESS", "")

# ─── Bankroll & Risk Management ──────────────────────────────────────
BANKROLL = None  # Must be fetched from Polymarket at startup

# Kelly Criterion
MAX_KELLY_FRACTION = 0.25      # Use quarter-Kelly (conservative)
MIN_EDGE_THRESHOLD = 0.08      # Minimum 8% edge to consider a trade
MIN_EDGE_FOR_LARGE = 0.12      # Edge required for larger positions

# Position limits
MAX_POSITION_PCT = 0.10        # Max 10% of bankroll on any single market
MAX_TOTAL_EXPOSURE = 0.60      # Max 60% of bankroll deployed at once
MAX_POSITIONS = 15             # Maximum number of concurrent positions

# Risk controls
DAILY_LOSS_LIMIT_PCT = 0.05   # Stop trading if down 5% in a day
MAX_DRAWDOWN_PCT = 0.15       # Circuit breaker at 15% drawdown from peak
CORRELATION_LIMIT = 3          # Max positions in same category

# ─── Market Filters ─────────────────────────────────────────────────
MIN_LIQUIDITY_USD = 5000       # Minimum market liquidity
MIN_VOLUME_24H = 1000          # Minimum 24h volume
MIN_TIME_TO_RESOLUTION_HOURS = 24    # Don't trade markets resolving < 24h
MAX_TIME_TO_RESOLUTION_DAYS = 90     # Don't trade markets > 90 days out

# Price filters - avoid extremes
MIN_PRICE = 0.05              # Don't buy below 5 cents (too speculative)
MAX_PRICE = 0.95              # Don't buy above 95 cents (too little upside)

# ─── Probability Estimation ──────────────────────────────────────────
# Weight given to market price as Bayesian prior
MARKET_PRIOR_WEIGHT = 0.3

# Categories and their base rate adjustments
CATEGORY_CONFIGS = {
    "politics": {
        "favorite_longshot_bias": 0.05,  # Markets tend to overprice longshots
        "recency_weight": 0.7,
    },
    "crypto": {
        "favorite_longshot_bias": 0.03,
        "recency_weight": 0.5,
    },
    "sports": {
        "favorite_longshot_bias": 0.08,
        "recency_weight": 0.6,
    },
    "science": {
        "favorite_longshot_bias": 0.02,
        "recency_weight": 0.4,
    },
    "culture": {
        "favorite_longshot_bias": 0.04,
        "recency_weight": 0.5,
    },
    "default": {
        "favorite_longshot_bias": 0.04,
        "recency_weight": 0.5,
    },
}

# ─── Execution ────────────────────────────────────────────────────────
ORDER_TYPE = "limit"            # Use limit orders (not market)
LIMIT_OFFSET = 0.01            # Place limit 1 cent better than target
ORDER_TIMEOUT_SECONDS = 300    # Cancel unfilled orders after 5 minutes
MAX_SLIPPAGE = 0.02            # Maximum 2% slippage tolerance

# ─── Scheduling ───────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 300    # Scan for opportunities every 5 minutes
POSITION_CHECK_SECONDS = 60    # Check positions every minute
REBALANCE_HOURS = 6            # Re-evaluate all positions every 6 hours

# ─── LLM Estimation ──────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "")
LLM_ESTIMATION_ENABLED = os.getenv("LLM_ESTIMATION_ENABLED", "true").lower() == "true"
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")
PERPLEXITY_MODEL = os.getenv("PERPLEXITY_MODEL", "sonar")
LLM_ESTIMATE_WEIGHT = float(os.getenv("LLM_ESTIMATE_WEIGHT", "0.35"))

# ─── Database (Railway PostgreSQL plugin provides DATABASE_URL) ──────
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ─── Logging & State ─────────────────────────────────────────────────
LOG_FILE = "trading.log"
STATE_FILE = "state.json"
TRADES_FILE = "trades.json"
DASHBOARD_DATA_FILE = "dashboard_data.json"

# ─── Railway / Health Check ──────────────────────────────────────────
PORT = int(os.getenv("PORT", "8080"))
HEALTH_CHECK_ENABLED = os.getenv("HEALTH_CHECK_ENABLED", "true").lower() == "true"

# ─── Dashboard ────────────────────────────────────────────────────────
DASHBOARD_PORT = PORT
DASHBOARD_REFRESH_SECONDS = 30
