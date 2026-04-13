# Prediction Market Trading System

An AI-powered prediction market trading system that identifies mispriced contracts on Polymarket and executes trades via their API.

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  Dashboard (React)                │
│         Live positions, P&L, opportunities        │
└──────────────────────┬──────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────┐
│              Trading Engine (Python)              │
│                                                   │
│  ┌─────────────┐  ┌──────────┐  ┌─────────────┐ │
│  │  Market Data │  │ Estimator│  │  Execution  │ │
│  │   Fetcher    │→ │  Model   │→ │   Engine    │ │
│  └─────────────┘  └──────────┘  └─────────────┘ │
│                                                   │
│  ┌─────────────┐  ┌──────────┐  ┌─────────────┐ │
│  │    Risk      │  │ Position │  │   Logger    │ │
│  │  Manager     │  │ Tracker  │  │             │ │
│  └─────────────┘  └──────────┘  └─────────────┘ │
└──────────────────────────────────────────────────┘
```

## Components

### 1. `market_fetcher.py`
Pulls active markets from Polymarket's CLOB API. Filters for liquid markets with sufficient volume.

### 2. `probability_estimator.py`
Estimates "fair" probabilities using:
- Historical base rates for similar event types
- Current market price as a Bayesian prior
- News sentiment analysis (optional, via web search)
- Time decay modeling for event resolution

### 3. `edge_finder.py`
Compares estimated probabilities to market prices. Flags opportunities where:
- Edge > configurable threshold (default 8%)
- Market has sufficient liquidity
- Time to resolution is within range

### 4. `risk_manager.py`
- Kelly criterion position sizing (fractional: 25% Kelly)
- Maximum position size per market (default 10% of bankroll)
- Maximum total exposure (default 60% of bankroll)
- Correlation-aware exposure limits
- Daily loss limit / drawdown circuit breaker

### 5. `executor.py`
Handles order placement via Polymarket CLOB API:
- Limit orders at favorable prices
- Order monitoring and cancellation
- Fill tracking

### 6. `position_tracker.py`
Tracks all open positions, calculates unrealized P&L, monitors for resolution.

### 7. `config.py`
All configurable parameters in one place.

### 8. `main.py`
Orchestrator that runs the full loop on a schedule.

## Setup

### Prerequisites
- Python 3.10+
- Node.js 18+ (for dashboard)
- Polymarket account with API access
- An Ethereum wallet (for Polymarket's CLOB)

### Installation

```bash
cd prediction-market-trader

# Python dependencies
pip install -r requirements.txt

# Set up your environment
cp .env.example .env
# Edit .env with your API keys and wallet info
```

### Configuration

Edit `config.py` to set:
- `BANKROLL`: Your starting capital
- `MAX_KELLY_FRACTION`: How aggressive (0.25 = quarter Kelly, conservative)
- `MIN_EDGE_THRESHOLD`: Minimum edge to trade (0.08 = 8%)
- `MAX_POSITION_PCT`: Max % of bankroll per trade
- `MAX_TOTAL_EXPOSURE`: Max % of bankroll deployed

### Running

```bash
# Run the trading engine
python main.py

# In another terminal, start the dashboard
# (Open the dashboard.html in a browser or serve it)
```

## Risk Warnings

- This is experimental software. You can and will lose money.
- Start with small amounts you can afford to lose entirely.
- Past edge does not guarantee future edge.
- Prediction markets have liquidity risk — you may not be able to exit.
- Never risk money you need.

## How Edge Is Generated

The core thesis: prediction markets are populated largely by retail participants
who systematically misprice certain event types. Common biases include:
- **Favorite-longshot bias**: Overpricing unlikely outcomes
- **Recency bias**: Overweighting recent events
- **Narrative bias**: Pricing stories rather than base rates
- **Time decay neglect**: Not adjusting for changing resolution windows

The system attempts to exploit these by anchoring to base rates and adjusting
methodically rather than emotionally.
