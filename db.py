"""
Database persistence layer using PostgreSQL.
Stores trade state, positions, and portfolio snapshots.

Railway provides DATABASE_URL automatically when you add the PostgreSQL plugin.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras

import config

logger = logging.getLogger(__name__)

_conn = None


def get_connection():
    """Get or create a database connection."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(config.DATABASE_URL)
        _conn.autocommit = True
    return _conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value JSONB NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id SERIAL PRIMARY KEY,
                market_id TEXT NOT NULL,
                question TEXT,
                category TEXT,
                direction TEXT,
                num_shares INTEGER,
                entry_price DOUBLE PRECISION,
                total_cost DOUBLE PRECISION,
                estimated_prob DOUBLE PRECISION,
                market_prob_at_entry DOUBLE PRECISION,
                edge_at_entry DOUBLE PRECISION,
                order_id TEXT,
                token_id TEXT,
                opened_at TIMESTAMPTZ,
                closed_at TIMESTAMPTZ,
                settlement_price DOUBLE PRECISION,
                outcome TEXT,
                pnl DOUBLE PRECISION,
                return_pct DOUBLE PRECISION,
                is_open BOOLEAN DEFAULT TRUE,
                raw_data JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_trades_open ON trades (is_open)
            WHERE is_open = TRUE
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_trades_market ON trades (market_id)
        """)
    logger.info("Database initialized")


def save_state(key: str, value: dict):
    """Save a key-value pair to the state table."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO state (key, value, updated_at)
               VALUES (%s, %s, NOW())
               ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = NOW()""",
            (key, json.dumps(value), json.dumps(value)),
        )


def load_state(key: str) -> Optional[dict]:
    """Load a value from the state table."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM state WHERE key = %s", (key,))
        row = cur.fetchone()
        if row:
            return row[0] if isinstance(row[0], dict) else json.loads(row[0])
    return None


def save_trade(position: dict):
    """Insert a new open trade."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO trades
               (market_id, question, category, direction, num_shares, entry_price,
                total_cost, estimated_prob, market_prob_at_entry, edge_at_entry,
                order_id, token_id, opened_at, is_open, raw_data)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s)
               RETURNING id""",
            (
                position.get("market_id"),
                position.get("question"),
                position.get("category"),
                position.get("direction"),
                position.get("num_shares"),
                position.get("entry_price"),
                position.get("total_cost"),
                position.get("estimated_prob"),
                position.get("market_prob_at_entry"),
                position.get("edge_at_entry"),
                position.get("order_id"),
                position.get("token_id"),
                position.get("opened_at"),
                json.dumps(position),
            ),
        )
        trade_id = cur.fetchone()[0]
        logger.info(f"Trade saved to DB: id={trade_id} market={position.get('market_id')}")
        return trade_id


def close_trade(market_id: str, outcome: str, settlement_price: float, pnl: float, return_pct: float):
    """Mark a trade as closed."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE trades SET
               is_open = FALSE, closed_at = NOW(), outcome = %s,
               settlement_price = %s, pnl = %s, return_pct = %s
               WHERE market_id = %s AND is_open = TRUE""",
            (outcome, settlement_price, pnl, return_pct, market_id),
        )
        logger.info(f"Trade closed in DB: market={market_id} pnl={pnl:.2f}")


def get_open_trades() -> list[dict]:
    """Get all open trades."""
    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT raw_data FROM trades WHERE is_open = TRUE ORDER BY opened_at")
        return [row["raw_data"] for row in cur.fetchall()]


def get_closed_trades(limit: int = 50) -> list[dict]:
    """Get recent closed trades."""
    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT market_id, question, category, direction, num_shares,
                      entry_price, total_cost, estimated_prob, market_prob_at_entry,
                      edge_at_entry, order_id, token_id, opened_at, closed_at,
                      settlement_price, outcome, pnl, return_pct
               FROM trades WHERE is_open = FALSE
               ORDER BY closed_at DESC LIMIT %s""",
            (limit,),
        )
        rows = cur.fetchall()
        return [dict(row) for row in rows]


# ─── Leader Lock ─────────────────────────────────────────────────────
# Uses PostgreSQL advisory locks. Only one instance can hold the lock.
# The lock is automatically released when the connection closes (crash, redeploy).

LEADER_LOCK_ID = 8675309  # Arbitrary fixed ID for the trading lock


def try_acquire_leader_lock() -> bool:
    """
    Try to acquire the trading leader lock.
    Returns True if this instance is now the leader (can trade).
    Non-blocking: returns False immediately if another instance holds the lock.
    """
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (LEADER_LOCK_ID,))
        acquired = cur.fetchone()[0]
    if acquired:
        logger.info("Acquired leader lock — this instance will trade")
    else:
        logger.info("Leader lock held by another instance — running in scan-only mode")
    return acquired


def release_leader_lock():
    """Release the trading leader lock."""
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (LEADER_LOCK_ID,))
        logger.info("Released leader lock")
    except Exception as e:
        logger.warning(f"Failed to release leader lock: {e}")


def get_trade_stats() -> dict:
    """Get aggregate trade statistics."""
    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE is_open = TRUE) AS open_count,
                COUNT(*) FILTER (WHERE is_open = FALSE) AS closed_count,
                COUNT(*) FILTER (WHERE is_open = FALSE AND pnl > 0) AS wins,
                COUNT(*) FILTER (WHERE is_open = FALSE AND pnl <= 0) AS losses,
                COALESCE(SUM(pnl) FILTER (WHERE is_open = FALSE), 0) AS total_pnl,
                COALESCE(AVG(pnl) FILTER (WHERE is_open = FALSE AND pnl > 0), 0) AS avg_win,
                COALESCE(AVG(pnl) FILTER (WHERE is_open = FALSE AND pnl <= 0), 0) AS avg_loss,
                COALESCE(SUM(total_cost) FILTER (WHERE is_open = TRUE), 0) AS total_exposure
            FROM trades
        """)
        return dict(cur.fetchone())
