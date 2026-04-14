import { query } from './client.js';

export async function runMigrations() {
  await query(`
    CREATE TABLE IF NOT EXISTS trades (
      id SERIAL PRIMARY KEY,
      token_id TEXT NOT NULL,
      market_id TEXT,
      question TEXT,
      category TEXT,
      direction TEXT,
      shares DOUBLE PRECISION,
      entry_price DOUBLE PRECISION,
      exit_price DOUBLE PRECISION,
      cost DOUBLE PRECISION,
      revenue DOUBLE PRECISION,
      pnl DOUBLE PRECISION,
      status TEXT DEFAULT 'open',
      adapter TEXT,
      opened_at TIMESTAMPTZ DEFAULT NOW(),
      closed_at TIMESTAMPTZ,
      tp_order_id TEXT,
      sl_order_id TEXT
    )
  `);

  await query(`
    CREATE TABLE IF NOT EXISTS markets (
      market_id TEXT PRIMARY KEY,
      question TEXT,
      category TEXT,
      slug TEXT,
      yes_price DOUBLE PRECISION,
      no_price DOUBLE PRECISION,
      yes_token TEXT,
      no_token TEXT,
      liquidity DOUBLE PRECISION,
      volume DOUBLE PRECISION,
      end_date TEXT,
      is_live BOOLEAN DEFAULT FALSE,
      updated_at TIMESTAMPTZ DEFAULT NOW()
    )
  `);

  await query(`
    CREATE TABLE IF NOT EXISTS settings (
      key TEXT PRIMARY KEY,
      value JSONB NOT NULL,
      updated_at TIMESTAMPTZ DEFAULT NOW()
    )
  `);

  // Add status column if missing (old Python schema didn't have it)
  await query(`ALTER TABLE trades ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'open'`).catch(() => {});
  await query(`ALTER TABLE trades ADD COLUMN IF NOT EXISTS adapter TEXT`).catch(() => {});
  await query(`ALTER TABLE trades ADD COLUMN IF NOT EXISTS tp_order_id TEXT`).catch(() => {});
  await query(`ALTER TABLE trades ADD COLUMN IF NOT EXISTS sl_order_id TEXT`).catch(() => {});
  await query(`ALTER TABLE trades ADD COLUMN IF NOT EXISTS cost DOUBLE PRECISION`).catch(() => {});
  await query(`ALTER TABLE trades ADD COLUMN IF NOT EXISTS revenue DOUBLE PRECISION`).catch(() => {});
  await query(`ALTER TABLE trades ADD COLUMN IF NOT EXISTS exit_price DOUBLE PRECISION`).catch(() => {});
  await query(`ALTER TABLE trades ADD COLUMN IF NOT EXISTS shares DOUBLE PRECISION`).catch(() => {});

  await query(`CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status) WHERE status = 'open'`).catch(() => {});
  await query(`CREATE INDEX IF NOT EXISTS idx_trades_token_new ON trades (token_id)`).catch(() => {});

  console.log('Migrations complete');
}
