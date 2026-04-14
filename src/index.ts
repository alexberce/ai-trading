import { config } from './config.js';
import { runMigrations } from './db/migrations.js';
import { query } from './db/client.js';
import * as orderManager from './core/orderManager.js';
import { fetchPositions, fetchPortfolioValue } from './core/portfolio.js';
import { GammaFeed } from './feeds/gammaFeed.js';
import { SportsFeed } from './feeds/sportsFeed.js';
import { PriceFeed } from './feeds/priceFeed.js';
import { SportsAdapter } from './adapters/sports/index.js';
import { EsportsAdapter } from './adapters/esports/index.js';
import { CryptoAdapter } from './adapters/crypto/index.js';
import type { Adapter, Market, PriceChange, GameState } from './adapters/base.js';
import { startServer, broadcast, setAdapterToggle, setAdapterList, setMarketProvider } from './server.js';

async function main() {
  let cachedMarkets: Market[] = [];

  console.log('=== Polymarket Trading Bot (TypeScript) ===');
  console.log(`Trading: ${config.tradingEnabled ? 'ENABLED' : 'DISABLED'}`);
  console.log(`Proxy: ${config.proxyUrl ? config.proxyUrl.split('@').pop() : 'none'}`);

  // DB
  if (config.databaseUrl) {
    await runMigrations();
  }

  // Load existing positions
  const positions = await fetchPositions();
  await orderManager.loadOwnedTokens(positions);
  console.log(`Positions: ${positions.length}, Value: $${(await fetchPortfolioValue()).toFixed(2)}`);

  // Initialize adapters
  const adapters: Adapter[] = [
    new SportsAdapter(config.adapters.sports),
    new EsportsAdapter(config.adapters.esports),
    new CryptoAdapter(config.adapters.crypto),
  ];
  console.log(`Adapters: ${adapters.map(a => `${a.name}(${a.config.enabled ? 'ON' : 'OFF'})`).join(', ')}`);

  // Build category -> adapter mapping
  const categoryMap = new Map<string, Adapter[]>();
  for (const adapter of adapters) {
    for (const cat of adapter.categories) {
      const list = categoryMap.get(cat) ?? [];
      list.push(adapter);
      categoryMap.set(cat, list);
    }
  }

  // Route events to adapters
  function routeMarket(market: Market) {
    const adapterList = categoryMap.get(market.category) ?? [];
    for (const a of adapterList) a.onMarket(market);
  }

  function routePriceChange(change: PriceChange) {
    for (const a of adapters) a.onPriceChange(change);
  }

  function routeGameUpdate(game: GameState) {
    for (const a of adapters) a.onGameUpdate(game);
  }

  // Start feeds
  const gammaFeed = new GammaFeed();
  const sportsFeed = new SportsFeed();
  const priceFeed = new PriceFeed();

  sportsFeed.onGame(routeGameUpdate);
  priceFeed.onPrice(routePriceChange);

  sportsFeed.start();
  priceFeed.start();

  // Initial market discovery + subscribe to prices
  const markets = await gammaFeed.fetchActiveMarkets();
  cachedMarkets = [...markets];
  const tokenIds: string[] = [];
  for (const m of markets) {
    routeMarket(m);
    if (m.yesToken) tokenIds.push(m.yesToken);
    if (m.noToken) tokenIds.push(m.noToken);
  }
  priceFeed.subscribe(tokenIds);
  console.log(`Subscribed to ${tokenIds.length} token prices`);

  // Periodic tasks
  setInterval(async () => {
    // Refresh markets every 60s
    try {
      const fresh = await gammaFeed.fetchActiveMarkets();
      cachedMarkets = fresh;
      const newTokens: string[] = [];
      for (const m of fresh) {
        routeMarket(m);
        if (m.yesToken && !priceFeed.getPrice(m.yesToken)) newTokens.push(m.yesToken);
        if (m.noToken && !priceFeed.getPrice(m.noToken)) newTokens.push(m.noToken);
      }
      if (newTokens.length) priceFeed.subscribe(newTokens);
    } catch (e: any) {
      console.error(`Market refresh error: ${e.message}`);
    }
  }, 60_000);

  setInterval(async () => {
    // Sync positions every 30s
    try {
      const pos = await fetchPositions();
      // Detect closed positions
      const currentTokens = new Set(pos.map(p => p.token_id));
      for (const p of positions) {
        if (!currentTokens.has(p.token_id)) {
          orderManager.releaseToken(p.token_id);
          console.log(`Position closed: ${p.question.slice(0, 40)} PnL: $${p.pnl.toFixed(2)}`);
        }
      }
      // Update reference
      positions.length = 0;
      positions.push(...pos);
    } catch (e: any) {
      console.error(`Position sync error: ${e.message}`);
    }
  }, 10_000);

  setInterval(async () => {
    // Adapter ticks every 5s
    for (const a of adapters) {
      if (a.config.enabled) {
        try { await a.onTick(); } catch (e: any) {
          console.error(`${a.name} tick error: ${e.message}`);
        }
      }
    }
  }, 5_000);

  // Wire adapter toggles to server
  setAdapterToggle(async (name, enabled) => {
    const adapter = adapters.find(a => a.name === name);
    if (adapter) {
      adapter.setEnabled(enabled);
      console.log(`Adapter ${name} ${enabled ? 'enabled' : 'disabled'}`);
      // Persist to DB
      if (config.databaseUrl) {
        try {
          const states: Record<string, boolean> = {};
          for (const a of adapters) states[a.name] = a.config.enabled;
          await query(
            `INSERT INTO settings (key, value, updated_at) VALUES ('adapter_states', $1, NOW())
             ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()`,
            [JSON.stringify(states)]
          );
        } catch {}
      }
    }
  });

  setAdapterList(() => adapters.map(a => ({ name: a.name, enabled: a.config.enabled })));

  setMarketProvider(() => cachedMarkets.map(m => ({
    market_id: m.marketId,
    question: m.question,
    category: m.category,
    yes_price: m.yesPrice,
    no_price: m.noPrice,
    liquidity: m.liquidity,
    volume: m.volume24h,
    end_date: m.endDate,
  })));

  // Load adapter enabled state from DB
  if (config.databaseUrl) {
    try {
      const result = await query(`SELECT value FROM settings WHERE key = 'adapter_states'`);
      if (result.rows.length > 0) {
        const states = result.rows[0].value as Record<string, boolean>;
        for (const adapter of adapters) {
          if (states[adapter.name] !== undefined) {
            adapter.setEnabled(states[adapter.name]);
          }
        }
        console.log('Loaded adapter states from DB');
      }
    } catch {}
  }

  // HTTP server + dashboard
  startServer();

  // Broadcast dashboard updates every 5s with actual data
  setInterval(async () => {
    try {
      const { buildDashboardPayload } = await import('./server.js');
      const payload = await buildDashboardPayload();
      broadcast('dashboard', payload);
    } catch {}
  }, 5_000);

  console.log(`\nBot running. Watching ${markets.length} markets across ${adapters.length} adapters.`);
}

main().catch(console.error);
