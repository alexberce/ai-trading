import 'dotenv/config';

export const config = {
  // Polymarket
  polyApiKey: process.env.POLY_API_KEY ?? '',
  polyApiSecret: process.env.POLY_API_SECRET ?? '',
  polyApiPassphrase: process.env.POLY_API_PASSPHRASE ?? '',
  privateKey: process.env.PRIVATE_KEY ?? '',
  walletAddress: process.env.WALLET_ADDRESS ?? '',
  proxyWalletAddress: process.env.PROXY_WALLET_ADDRESS ?? '',

  // Proxy
  proxyUrl: process.env.PROXY_URL ?? '',

  // Database
  databaseUrl: process.env.DATABASE_URL ?? '',

  // Trading
  tradingEnabled: process.env.TRADING_ENABLED === 'true',
  initialDeposit: parseFloat(process.env.INITIAL_DEPOSIT ?? '300'),

  // Adapter settings
  adapters: {
    sports: { enabled: true, tp: 0.05, sl: 0.15, maxPosition: 20, maxConcurrent: 10 },
    esports: { enabled: true, tp: 0.03, sl: 0.10, maxPosition: 15, maxConcurrent: 10 },
    crypto: { enabled: true, tp: 0.05, sl: 0.15, maxPosition: 20, maxConcurrent: 10 },
    politics: { enabled: false, tp: 0.08, sl: 0.20, maxPosition: 20, maxConcurrent: 5 },
  },

  // Server
  port: parseInt(process.env.PORT ?? '8080'),

  // API endpoints
  clobApi: 'https://clob.polymarket.com',
  gammaApi: 'https://gamma-api.polymarket.com',
  dataApi: 'https://data-api.polymarket.com',
  sportsWs: 'wss://sports-api.polymarket.com/ws',
  marketWs: 'wss://ws-subscriptions-clob.polymarket.com/ws/market',
} as const;
