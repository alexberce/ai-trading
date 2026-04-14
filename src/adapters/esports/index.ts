import { Adapter, AdapterConfig, Market, PriceChange, GameState } from '../base.js';
import * as orderManager from '../../core/orderManager.js';
import { config } from '../../config.js';

/**
 * Esports Adapter — trades live esports markets (CS2, LoL, Dota2).
 *
 * Esports are FAST — rounds/maps flip in seconds.
 * Tighter TP (3%), tighter SL (10%), react to map/round changes.
 */
export class EsportsAdapter implements Adapter {
  readonly name = 'esports';
  readonly categories = ['cs2', 'lol', 'dota2', 'valorant'];
  config: AdapterConfig;

  private tokenToMarket = new Map<string, Market>();
  private liveGames = new Set<string>();

  constructor(adapterConfig: AdapterConfig) {
    this.config = adapterConfig;
  }

  onMarket(market: Market) {
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

    if (change.newPrice < 0.20 || change.newPrice > 0.80) return;

    // Esports: react to bigger dips because volatility is higher
    if (change.change < -0.02) { // >2% drop
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
          console.log(`[Esports] Bought dip: ${market.question.slice(0, 40)}`);
        }
      }).catch(e => console.error(`[Esports] Buy error: ${e.message}`));
    }
  }

  async onTick() {}

  setEnabled(enabled: boolean) {
    this.config.enabled = enabled;
    console.log(`[Esports] ${enabled ? 'Enabled' : 'Disabled'}`);
  }
}
