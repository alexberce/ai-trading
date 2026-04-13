"""Test the scalper's market discovery and signal detection locally."""
import os
os.environ["LLM_ESTIMATION_ENABLED"] = "false"
os.environ["TRADING_ENABLED"] = "false"
os.environ["SCALP_ENABLED"] = "true"

import json
import requests
from datetime import datetime, timezone

# Simulate what the scalper does
print("=== Testing Scalper Logic ===\n")

# 1. Fetch recent events
print("1. Fetching recent events...")
resp = requests.get(
    "https://gamma-api.polymarket.com/events",
    params={"active": "true", "closed": "false", "limit": 50,
            "order": "startDate", "ascending": "false"},
    headers={"User-Agent": "Mozilla/5.0"},
    timeout=15,
)
events = resp.json()
print(f"   Got {len(events)} events")

# 2. Extract markets
all_markets = []
for event in events:
    for m in event.get("markets", []):
        m["_event"] = event.get("title", "")
        m["_event_slug"] = event.get("slug", "")
        all_markets.append(m)
print(f"   Got {len(all_markets)} total markets")

# 3. Filter like the scalper does
now = datetime.now(timezone.utc)
active = []
for m in all_markets:
    h_change = float(m.get("oneHourPriceChange") or 0)
    d_change = float(m.get("oneDayPriceChange") or 0)
    vol24 = float(m.get("volume24hr") or 0)
    liq = float(m.get("liquidity") or 0)

    if liq < 1000:
        continue

    prices = m.get("outcomePrices", "[]")
    if isinstance(prices, str):
        try: prices = json.loads(prices)
        except: prices = []
    yes_price = float(prices[0]) if prices else 0

    if yes_price < 0.05 or yes_price > 0.95:
        continue

    token_ids = m.get("clobTokenIds", "[]")
    if isinstance(token_ids, str):
        try: token_ids = json.loads(token_ids)
        except: token_ids = []
    if not token_ids or len(token_ids) < 2:
        continue

    end = m.get("endDateIso", "")
    hours_left = None
    if end:
        try:
            end_dt = datetime.fromisoformat(end + "T23:59:59+00:00") if len(end) == 10 else datetime.fromisoformat(end.replace("Z", "+00:00"))
            hours_left = (end_dt - now).total_seconds() / 3600
        except:
            pass

    if hours_left is None or hours_left > 48 or hours_left < 0:
        continue

    active.append({
        "question": m.get("question", ""),
        "yes_price": yes_price,
        "h_change": h_change,
        "d_change": d_change,
        "vol24": vol24,
        "liq": liq,
        "hours_left": hours_left,
        "event": m.get("_event", ""),
    })

print(f"\n2. Active markets (ending <48h): {len(active)}")
for m in active[:15]:
    print(f"   {m['question'][:55]:55} h={m['h_change']:+.3f} d={m['d_change']:+.3f} vol24={m['vol24']:>8,.0f} price={m['yes_price']:.2f} hrs={m['hours_left']:.0f}")

# 4. Check signals
THRESHOLD = 0.01
signals = []
for m in active:
    h = m["h_change"]
    d = m["d_change"]
    vol24 = m["vol24"]

    if h < -THRESHOLD:
        signals.append({"market": m["question"][:50], "type": "dip_yes", "h": h, "d": d})
    elif h > THRESHOLD:
        signals.append({"market": m["question"][:50], "type": "dip_no", "h": h, "d": d})
    elif d < -0.03 and vol24 > 5000:
        signals.append({"market": m["question"][:50], "type": "momentum_yes", "h": h, "d": d})
    elif d > 0.03 and vol24 > 5000:
        signals.append({"market": m["question"][:50], "type": "momentum_no", "h": h, "d": d})

print(f"\n3. Signals found: {len(signals)}")
for s in signals:
    print(f"   [{s['type']:12}] {s['market']:50} h={s['h']:+.3f} d={s['d']:+.3f}")

if not signals:
    print("\n   NO SIGNALS! Possible reasons:")
    print(f"   - Threshold too high ({THRESHOLD})")
    print(f"   - No hourly price changes on any market")
    print(f"   - Markets ending <48h have no h_change data")
    print()
    print("   All h_change values:")
    for m in active[:20]:
        print(f"     {m['question'][:45]:45} h={m['h_change']:+.4f} d={m['d_change']:+.4f}")
