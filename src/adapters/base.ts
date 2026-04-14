export interface Market {
  marketId: string;
  question: string;
  category: string;
  slug: string;
  yesPrice: number;
  noPrice: number;
  yesToken: string;
  noToken: string;
  liquidity: number;
  volume24h: number;
  endDate: string;
  hoursLeft: number | null;
}

export interface GameState {
  slug: string;
  live: boolean;
  ended: boolean;
  score: string;
  period: string;
  elapsed?: string;
}

export interface PriceChange {
  tokenId: string;
  newPrice: number;
  oldPrice: number;
  change: number; // percentage
}

export interface AdapterConfig {
  enabled: boolean;
  tp: number;       // take profit %
  sl: number;       // stop loss %
  maxPosition: number;  // max $ per trade
  maxConcurrent: number;
}

/**
 * Base adapter interface. Each category (sports, esports, crypto)
 * implements this to define its own trading logic.
 */
export interface Adapter {
  readonly name: string;
  readonly categories: string[]; // e.g. ['mlb', 'nhl', 'nba'] for sports
  config: AdapterConfig;

  /** Called when a market matching this adapter's categories is discovered */
  onMarket(market: Market): void;

  /** Called on every real-time price change from WebSocket */
  onPriceChange(change: PriceChange): void;

  /** Called on every game state change from Sports WebSocket */
  onGameUpdate(game: GameState): void;

  /** Called every N seconds for periodic logic */
  onTick(): Promise<void>;

  /** Enable/disable at runtime */
  setEnabled(enabled: boolean): void;
}
