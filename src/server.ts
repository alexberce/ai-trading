import http from 'http';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { config } from './config.js';
import { fetchPositions, fetchPortfolioValue } from './core/portfolio.js';
import * as orderManager from './core/orderManager.js';
import { query } from './db/client.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// SSE clients
const sseClients = new Set<http.ServerResponse>();

export function broadcast(event: string, data: any) {
  const msg = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const client of sseClients) {
    try {
      client.write(msg);
    } catch {
      sseClients.delete(client);
    }
  }
}

// Callbacks set by index.ts
let adapterToggle: ((name: string, enabled: boolean) => void) | null = null;
let getAdapters: () => Array<{ name: string; enabled: boolean }> = () => [];
let getMarkets: () => any[] = () => [];

export function setAdapterToggle(fn: (name: string, enabled: boolean) => void) {
  adapterToggle = fn;
}

export function setAdapterList(fn: () => Array<{ name: string; enabled: boolean }>) {
  getAdapters = fn;
}

export function setMarketProvider(fn: () => any[]) {
  getMarkets = fn;
}

export async function buildDashboardPayload() {
  const positions = await fetchPositions();
  const realPositions = positions.filter(p => (p.current_value || p.total_cost || 0) >= 1);
  const portfolioValue = await fetchPortfolioValue();

  const unrealizedPnl = realPositions.reduce((sum, p) => sum + (p.pnl || 0), 0);
  const totalValue = portfolioValue || realPositions.reduce((sum, p) => sum + (p.current_value || 0), 0);
  const totalReturn = config.initialDeposit > 0 ? (totalValue - config.initialDeposit) / config.initialDeposit : 0;

  // Trade stats from DB
  let tradeStats = { wins: 0, losses: 0, totalPnl: 0 };
  if (config.databaseUrl) {
    try {
      const result = await query(`
        SELECT
          COUNT(*) FILTER (WHERE pnl > 0) as wins,
          COUNT(*) FILTER (WHERE pnl <= 0 AND pnl IS NOT NULL) as losses,
          COALESCE(SUM(pnl), 0) as total_pnl
        FROM trades WHERE (status = 'closed' OR is_open = FALSE)
      `);
      if (result.rows[0]) {
        tradeStats = {
          wins: parseInt(result.rows[0].wins),
          losses: parseInt(result.rows[0].losses),
          totalPnl: parseFloat(result.rows[0].total_pnl),
        };
      }
    } catch {}
  }

  const closedCount = tradeStats.wins + tradeStats.losses;

  // Closed trades for history
  let closedTrades: any[] = [];
  if (config.databaseUrl) {
    try {
      const result = await query(`
        SELECT question, direction, pnl, return_pct, closed_at, entry_price, settlement_price as exit_price
        FROM trades WHERE (status = 'closed' OR is_open = FALSE)
        ORDER BY closed_at DESC NULLS LAST LIMIT 50
      `);
      closedTrades = result.rows;
    } catch {}
  }

  return {
    updated_at: new Date().toISOString(),
    portfolio: {
      bankroll: Math.round(totalValue * 100) / 100,
      initial_bankroll: config.initialDeposit,
      total_return: Math.round(totalReturn * 10000) / 10000,
      total_pnl_closed: Math.round(tradeStats.totalPnl * 100) / 100,
      unrealized_pnl: Math.round(unrealizedPnl * 100) / 100,
      open_positions: realPositions.length,
      total_exposure: Math.round(realPositions.reduce((s, p) => s + (p.current_value || 0), 0) * 100) / 100,
      exposure_pct: totalValue > 0 ? Math.round(realPositions.reduce((s, p) => s + (p.current_value || 0), 0) / totalValue * 10000) / 10000 : 0,
      closed_trades: closedCount,
      wins: tradeStats.wins,
      losses: tradeStats.losses,
      win_rate: closedCount > 0 ? Math.round(tradeStats.wins / closedCount * 10000) / 10000 : 0,
      is_halted: false,
      halt_reason: '',
    },
    open_positions: realPositions,
    closed_positions: closedTrades,
    opportunities: [],
    scanned_markets: [],
    all_markets: getMarkets(),
    banned_markets: [],
    adapters: getAdapters(),
  };
}

function handleRequest(req: http.IncomingMessage, res: http.ServerResponse) {
  const url = req.url ?? '/';

  // CORS
  res.setHeader('Access-Control-Allow-Origin', '*');

  if (req.method === 'GET') {
    if (url === '/' || url === '/markets' || url === '/positions' || url === '/signals' || url === '/history') {
      // Serve dashboard for all page routes (SPA)
      const htmlPath = path.join(__dirname, '..', 'src', 'dashboard', 'index.html');
      if (fs.existsSync(htmlPath)) {
        res.writeHead(200, { 'Content-Type': 'text/html' });
        fs.createReadStream(htmlPath).pipe(res);
      } else {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'ok', message: 'Dashboard not found' }));
      }
    } else if (url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok' }));
    } else if (url === '/api/dashboard') {
      buildDashboardPayload().then(data => {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(data));
      }).catch(err => {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: err.message }));
      });
    } else if (url === '/api/events') {
      // SSE
      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
      });
      sseClients.add(res);

      // Send current state
      buildDashboardPayload().then(data => {
        res.write(`event: dashboard\ndata: ${JSON.stringify(data)}\n\n`);
      }).catch(() => {});

      // Keepalive
      const interval = setInterval(() => {
        try { res.write(': keepalive\n\n'); } catch { clearInterval(interval); }
      }, 15000);

      req.on('close', () => {
        sseClients.delete(res);
        clearInterval(interval);
      });
    } else {
      res.writeHead(404);
      res.end();
    }
  } else if (req.method === 'POST') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', async () => {
      const data = body ? JSON.parse(body) : {};

      if (url === '/api/close') {
        const { token_id, size, price } = data;
        if (token_id && size > 0 && price > 0) {
          const result = await orderManager.sell(token_id, price, size, 'manual_close');
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify(result));
        } else {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'token_id, size, price required' }));
        }
      } else if (url === '/api/cancel-all') {
        await orderManager.cancelAll();
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, action: 'all_orders_cancelled' }));
      } else if (url === '/api/adapter') {
        // Toggle adapter on/off: { name: "sports", enabled: true }
        const { name, enabled } = data;
        if (name && adapterToggle) {
          adapterToggle(name, enabled);
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ ok: true, name, enabled }));
        } else {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'name and enabled required' }));
        }
      } else {
        res.writeHead(404);
        res.end();
      }
    });
  } else {
    res.writeHead(405);
    res.end();
  }
}

export function startServer() {
  const server = http.createServer(handleRequest);
  server.listen(config.port, () => {
    console.log(`Server running on port ${config.port}`);
  });
  return server;
}
