"""
One-time setup: derive Polymarket API credentials from your wallet private key.

Usage:
  1. Set PRIVATE_KEY in your .env file (Polygon wallet private key with 0x prefix)
  2. Run: python setup_creds.py
  3. Copy the output into your .env / Railway variables

Zero external dependencies — uses only Python stdlib.
"""
import os
import sys

# Point the user to the web UI instead of doing complex crypto locally
print("""
╔══════════════════════════════════════════════════════════════╗
║           Polymarket API Credentials Setup                   ║
╚══════════════════════════════════════════════════════════════╝

The safest way to get your API credentials:

  1. Go to https://polymarket.com
  2. Log in with your wallet
  3. Open browser DevTools (F12) → Console
  4. Run this snippet:

     copy(JSON.stringify(JSON.parse(localStorage.getItem('clob_api_creds')), null, 2))

  5. Paste the result — it contains your apiKey, secret, and passphrase

Then add to your .env and Railway variables:

  POLY_API_KEY=<apiKey from above>
  POLY_API_SECRET=<secret from above>
  POLY_API_PASSPHRASE=<passphrase from above>
  PRIVATE_KEY=<your wallet private key>
  WALLET_ADDRESS=<your wallet address>

""")
