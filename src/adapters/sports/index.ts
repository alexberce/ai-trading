import { Adapter, AdapterConfig, Market, PriceChange, GameState } from '../base.js';
import * as orderManager from '../../core/orderManager.js';
import { config } from '../../config.js';

/**
 * Sports Adapter — trades live sports markets (MLB, NHL, NBA, soccer).
 *
 * Strategy:
 * - Watches for price dips during live games (scoring events cause volatility)
 * - Buys limit order when price drops >1% in real-time
 * - TP sell at entry + 5% placed immediately
 * - Only trades markets where the game is currently live
 * - Avoids longshots (<20%) and near-certainties (>80%)
 */
export class SportsAdapter implements Adapter {
  readonly name = 'sports';
  readonly categories = ['mlb', 'nhl', 'nba', 'epl', 'ucl', 'mls', 'nfl'];
  config: AdapterConfig;

  private markets = new Map<string, Market>();     // marketId -> market
  private tokenToMarket = new Map<string, Market>(); // tokenId -> market
  private liveGames = new Set<string>();           // slugs of live games
  private priceHistory = new Map<string, number[]>(); // tokenId -> recent prices

  constructor(adapterConfig: AdapterConfig) {
    this.config = adapterConfig;
  }

  onMarket(market: Market) {
    this.markets.set(market.marketId, market);
    if (market.yesToken) this.tokenToMarket.set(market.yesToken, market);
    if (market.noToken) this.tokenToMarket.set(market.noToken, market);
  }

  onGameUpdate(game: GameState) {
    if (game.live && !game.ended) {
      this.liveGames.add(game.slug);
    } else {
      this.liveGames.delete(game.slug);
    }
  }

  onPriceChange(change: PriceChange) {
    if (!this.config.enabled || !config.tradingEnabled) return;

    const market = this.tokenToMarket.get(change.tokenId);
    if (!market) return;

    // Only trade if the game is live
    const isLive = this.isGameLive(market.slug);
    if (!isLive) return;

    // Track price history
    const history = this.priceHistory.get(change.tokenId) ?? [];
    history.push(change.newPrice);
    if (history.length > 20) history.shift();
    this.priceHistory.set(change.tokenId, history);

    // Skip extreme prices
    if (change.newPrice < 0.20 || change.newPrice > 0.80) return;

    // Signal: price dropped significantly
    if (change.change < -0.01) { // >1% drop
      const shares = Math.floor(this.config.maxPosition / change.newPrice);
      if (shares < 5) return;

      orderManager.buy(
        change.tokenId,
        change.newPrice,
        shares,
        market.question,
        this.name,
        this.config.tp,
      ).then(result => {
        if (result.success) {
          console.log(`[Sports] Bought dip: ${market.question.slice(0, 40)} ${result.shares}@$${result.price}`);
        }
      }).catch(e => console.error(`[Sports] Buy error: ${e.message}`));
    }
  }

  async onTick() {
    // Periodic logic — could check for SL exits here
  }

  setEnabled(enabled: boolean) {
    this.config.enabled = enabled;
    console.log(`[Sports] ${enabled ? 'Enabled' : 'Disabled'}`);
  }

  private isGameLive(slug: string): boolean {
    // Check if any live game slug is contained in the market slug
    for (const liveSlug of this.liveGames) {
      if (slug.includes(liveSlug) || liveSlug.includes(slug)) return true;
    }
    // Fallback: consider it live if no sports WS data (game might be in progress)
    return false;
  }
}
