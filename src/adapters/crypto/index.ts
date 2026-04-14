import { Adapter, AdapterConfig, Market, PriceChange, GameState } from '../base.js';
import * as orderManager from '../../core/orderManager.js';
import { config } from '../../config.js';

/**
 * Crypto Adapter — trades crypto price prediction markets.
 *
 * "Will BTC be above $75K on April 14?" type markets.
 * 5-minute resolution markets, follows BTC/ETH charts.
 */
export class CryptoAdapter implements Adapter {
  readonly name = 'crypto';
  readonly categories = ['crypto', 'btc', 'eth', 'sol', 'doge'];
  config: AdapterConfig;

  private tokenToMarket = new Map<string, Market>();

  constructor(adapterConfig: AdapterConfig) {
    this.config = adapterConfig;
  }

  onMarket(market: Market) {
    if (market.yesToken) this.tokenToMarket.set(market.yesToken, market);
    if (market.noToken) this.tokenToMarket.set(market.noToken, market);
  }

  onGameUpdate(_game: GameState) {
    // Crypto doesn't use game state
  }

  onPriceChange(change: PriceChange) {
    if (!this.config.enabled || !config.tradingEnabled) return;

    const market = this.tokenToMarket.get(change.tokenId);
    if (!market) return;

    if (change.newPrice < 0.20 || change.newPrice > 0.80) return;

    // Only trade markets resolving within 24h
    if (market.hoursLeft !== null && market.hoursLeft > 24) return;

    // Buy dips
    if (change.change < -0.015) { // >1.5% drop
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
          console.log(`[Crypto] Bought dip: ${market.question.slice(0, 40)}`);
        }
      }).catch(e => console.error(`[Crypto] Buy error: ${e.message}`));
    }
  }

  async onTick() {}

  setEnabled(enabled: boolean) {
    this.config.enabled = enabled;
    console.log(`[Crypto] ${enabled ? 'Enabled' : 'Disabled'}`);
  }
}
