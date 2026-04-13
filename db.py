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
    _init_scan_tables()
    _init_markets_table()
    _init_banned_table()
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


def load_state(key: str):
    """Load a value from the state table. Returns dict, list, or None."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM state WHERE key = %s", (key,))
        row = cur.fetchone()
        if row:
            val = row[0]
            if isinstance(val, (dict, list)):
                return val
            return json.loads(val)
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

    First clears any stale locks (from crashed instances), then tries to acquire.
    Uses pg_try_advisory_lock which is session-scoped — auto-releases on disconnect.
    """
    conn = get_connection()
    with conn.cursor() as cur:
        # Force-release ALL advisory locks (from any crashed session on this connection)
        cur.execute("SELECT pg_advisory_unlock_all()")
        # Also force-unlock our specific lock ID in case another session holds it
        # This is safe because we only run one trading instance at a time
        cur.execute("SELECT pg_advisory_unlock(%s)", (LEADER_LOCK_ID,))
        # Now acquire
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


# ─── Scan Results Persistence ────────────────────────────────────────

def _init_scan_tables():
    """Create scan-related tables. Called from init_db()."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scan_results (
                id SERIAL PRIMARY KEY,
                scanned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                markets_scanned INTEGER NOT NULL DEFAULT 0,
                estimates_with_edge INTEGER NOT NULL DEFAULT 0,
                opportunities_count INTEGER NOT NULL DEFAULT 0,
                scan_duration_seconds DOUBLE PRECISION
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS estimates (
                id SERIAL PRIMARY KEY,
                scan_id INTEGER REFERENCES scan_results(id) ON DELETE CASCADE,
                market_id TEXT NOT NULL,
                question TEXT,
                category TEXT,
                market_prob DOUBLE PRECISION,
                estimated_prob DOUBLE PRECISION,
                edge DOUBLE PRECISION,
                effective_edge DOUBLE PRECISION,
                confidence DOUBLE PRECISION,
                direction TEXT,
                has_edge BOOLEAN,
                reasoning TEXT,
                components JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id SERIAL PRIMARY KEY,
                scan_id INTEGER REFERENCES scan_results(id) ON DELETE CASCADE,
                market_id TEXT NOT NULL,
                question TEXT,
                category TEXT,
                direction TEXT,
                market_price DOUBLE PRECISION,
                estimated_prob DOUBLE PRECISION,
                edge DOUBLE PRECISION,
                effective_edge DOUBLE PRECISION,
                confidence DOUBLE PRECISION,
                score DOUBLE PRECISION,
                reasoning TEXT,
                sizing JSONB,
                liquidity DOUBLE PRECISION,
                volume_24h DOUBLE PRECISION,
                hours_to_resolution DOUBLE PRECISION,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)


def save_scan_results(markets_scanned: int, estimates_with_edge: int,
                      opportunities_count: int, duration: float) -> int:
    """Insert a scan_results row. Returns the scan_id."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO scan_results
               (markets_scanned, estimates_with_edge, opportunities_count, scan_duration_seconds)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (markets_scanned, estimates_with_edge, opportunities_count, duration),
        )
        scan_id = cur.fetchone()[0]
        # Keep only last 5 scans
        cur.execute(
            "DELETE FROM scan_results WHERE id < (SELECT MAX(id) - 4 FROM scan_results)"
        )
        return scan_id


def save_estimates(scan_id: int, estimates: list[dict]):
    """Bulk insert estimates for a scan."""
    if not estimates:
        return
    conn = get_connection()
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO estimates
               (scan_id, market_id, question, category, market_prob, estimated_prob,
                edge, effective_edge, confidence, direction, has_edge, reasoning, components)
               VALUES %s""",
            [
                (scan_id, e.get("market_id", ""), e.get("question", ""),
                 e.get("category", ""), e.get("market_prob", 0),
                 e.get("estimated_prob", 0), e.get("edge", 0),
                 e.get("effective_edge", 0), e.get("confidence", 0),
                 e.get("direction", ""), e.get("has_edge", False),
                 e.get("reasoning", ""),
                 json.dumps(e.get("components", {})))
                for e in estimates
            ],
        )


def save_opportunities(scan_id: int, opportunities: list[dict]):
    """Bulk insert opportunities for a scan."""
    if not opportunities:
        return
    conn = get_connection()
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO opportunities
               (scan_id, market_id, question, category, direction, market_price,
                estimated_prob, edge, effective_edge, confidence, score, reasoning,
                sizing, liquidity, volume_24h, hours_to_resolution)
               VALUES %s""",
            [
                (scan_id, o.get("market_id", ""), o.get("question", ""),
                 o.get("category", ""), o.get("direction", ""),
                 o.get("market_price", 0), o.get("estimated_prob", 0),
                 o.get("edge", 0), o.get("effective_edge", 0),
                 o.get("confidence", 0), o.get("score", 0),
                 o.get("reasoning", ""),
                 json.dumps(o.get("sizing", {})),
                 o.get("liquidity", 0), o.get("volume_24h", 0),
                 o.get("hours_to_resolution"))
                for o in opportunities
            ],
        )


def get_latest_scan() -> Optional[dict]:
    """Get the most recent scan result."""
    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM scan_results ORDER BY scanned_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_latest_estimates() -> list[dict]:
    """Get all estimates from the latest scan."""
    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT market_id, question, category, market_prob, estimated_prob,
                   edge, effective_edge, confidence, direction, has_edge, reasoning, components
            FROM estimates
            WHERE scan_id = (SELECT MAX(id) FROM scan_results)
            ORDER BY ABS(edge) DESC
            LIMIT 100
        """)
        return [dict(row) for row in cur.fetchall()]


def get_latest_opportunities() -> list[dict]:
    """Get all opportunities from the latest scan."""
    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT market_id, question, category, direction, market_price,
                   estimated_prob, edge, effective_edge, confidence, score,
                   reasoning, sizing, liquidity, volume_24h, hours_to_resolution
            FROM opportunities
            WHERE scan_id = (SELECT MAX(id) FROM scan_results)
            ORDER BY score DESC
        """)
        return [dict(row) for row in cur.fetchall()]


def save_scan_progress(done: int, total: int):
    """Save current scan progress to state table."""
    save_state("scan_progress", {"done": done, "total": total})


def get_scan_progress() -> Optional[dict]:
    """Get current scan progress."""
    return load_state("scan_progress")


# ─── Banned Markets ──────────────────────────────────────────────────

# ─── Markets Persistence ─────────────────────────────────────────────

def _init_markets_table():
    """Create markets table. Called from init_db()."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS markets (
                market_id TEXT PRIMARY KEY,
                condition_id TEXT,
                question TEXT,
                category TEXT,
                yes_price DOUBLE PRECISION,
                no_price DOUBLE PRECISION,
                liquidity DOUBLE PRECISION,
                volume DOUBLE PRECISION,
                end_date TEXT,
                active BOOLEAN DEFAULT TRUE,
                first_seen_at TIMESTAMPTZ DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)


def save_markets(markets: list[dict]):
    """Upsert markets — new ones get added, existing ones get updated."""
    if not markets:
        return
    conn = get_connection()
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO markets
               (market_id, condition_id, question, category, yes_price, no_price,
                liquidity, volume, end_date, active)
               VALUES %s
               ON CONFLICT (market_id) DO UPDATE SET
                 yes_price = EXCLUDED.yes_price,
                 no_price = EXCLUDED.no_price,
                 liquidity = EXCLUDED.liquidity,
                 volume = EXCLUDED.volume,
                 active = EXCLUDED.active,
                 last_seen_at = NOW()""",
            [
                (m.get("market_id", ""), m.get("condition_id", ""),
                 m.get("question", ""), m.get("category", ""),
                 m.get("yes_price", 0), m.get("no_price", 0),
                 m.get("liquidity", 0), m.get("volume", 0),
                 m.get("end_date", ""), True)
                for m in markets
            ],
        )


def get_all_markets(active_only: bool = True) -> list[dict]:
    """Get all markets from DB."""
    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        where = "WHERE active = TRUE" if active_only else ""
        cur.execute(f"""
            SELECT market_id, condition_id, question, category,
                   yes_price, no_price, liquidity, volume, end_date,
                   first_seen_at, last_seen_at
            FROM markets {where}
            ORDER BY liquidity DESC NULLS LAST
        """)
        return [dict(row) for row in cur.fetchall()]


def save_live_positions(positions: list[dict]):
    """Cache live positions in the state table."""
    save_state("live_positions", positions)


def get_live_positions() -> list[dict]:
    """Get cached live positions."""
    return load_state("live_positions") or []


def save_balance(balance_data: dict):
    """Cache balance data."""
    save_state("balance", balance_data)


def get_balance() -> dict:
    """Get cached balance."""
    return load_state("balance") or {"total": 0, "cash": 0, "positions_value": 0}


def _init_banned_table():
    """Create banned_markets table. Called from init_db()."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS banned_markets (
                market_id TEXT PRIMARY KEY,
                question TEXT,
                banned_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)


def ban_market(market_id: str, question: str = ""):
    """Ban a market from trading."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO banned_markets (market_id, question)
               VALUES (%s, %s) ON CONFLICT (market_id) DO NOTHING""",
            (market_id, question),
        )
    logger.info(f"Banned market: {market_id} ({question[:50]})")


def unban_market(market_id: str):
    """Remove a market from the ban list."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM banned_markets WHERE market_id = %s", (market_id,))
    logger.info(f"Unbanned market: {market_id}")


def get_banned_markets() -> set:
    """Get set of banned market IDs."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT market_id FROM banned_markets")
        return {row[0] for row in cur.fetchall()}


def get_banned_markets_list() -> list[dict]:
    """Get full list of banned markets."""
    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT market_id, question, banned_at FROM banned_markets ORDER BY banned_at DESC")
        return [dict(row) for row in cur.fetchall()]


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
