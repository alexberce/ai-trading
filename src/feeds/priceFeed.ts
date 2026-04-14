import WebSocket from 'ws';
import { config } from '../config.js';
import { PriceChange } from '../adapters/base.js';

type PriceHandler = (change: PriceChange) => void;

/**
 * Market WebSocket feed — real-time price changes.
 * Subscribe to token IDs, get instant price updates on every trade.
 */
export class PriceFeed {
  private ws: WebSocket | null = null;
  private handlers: PriceHandler[] = [];
  private subscribedTokens: string[] = [];
  private prices = new Map<string, number>();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  onPrice(handler: PriceHandler) {
    this.handlers.push(handler);
  }

  subscribe(tokenIds: string[]) {
    const newTokens = tokenIds.filter(t => !this.subscribedTokens.includes(t));
    if (!newTokens.length) return;
    this.subscribedTokens.push(...newTokens);

    if (this.ws?.readyState === WebSocket.OPEN) {
      this.sendSubscription(newTokens);
    }
  }

  getPrice(tokenId: string): number | undefined {
    return this.prices.get(tokenId);
  }

  start() {
    this.connect();
    console.log('PriceFeed started');
  }

  stop() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
  }

  private connect() {
    this.ws = new WebSocket(config.marketWs);

    this.ws.on('open', () => {
      console.log(`PriceFeed connected, subscribing to ${this.subscribedTokens.length} tokens`);
      if (this.subscribedTokens.length) {
        this.sendSubscription(this.subscribedTokens);
      }
    });

    this.ws.on('message', (data) => {
      const msg = data.toString();
      if (msg === 'PONG') return;

      try {
        const parsed = JSON.parse(msg);
        const items = Array.isArray(parsed) ? parsed : [parsed];

        for (const item of items) {
          const tokenId = item.asset_id;
          const price = parseFloat(item.price);
          if (!tokenId || !price) continue;

          const oldPrice = this.prices.get(tokenId);
          this.prices.set(tokenId, price);

          if (oldPrice && oldPrice !== price) {
            const change = (price - oldPrice) / oldPrice;
            if (Math.abs(change) > 0.001) { // >0.1% change
              const priceChange: PriceChange = { tokenId, newPrice: price, oldPrice, change };
              for (const h of this.handlers) h(priceChange);
            }
          }
        }
      } catch {}
    });

    this.ws.on('close', () => {
      console.log('PriceFeed disconnected, reconnecting in 5s');
      this.reconnectTimer = setTimeout(() => this.connect(), 5000);
    });

    this.ws.on('error', (err) => {
      console.warn(`PriceFeed error: ${err.message}`);
    });

    // Heartbeat
    const pingInterval = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send('PING');
      }
    }, 10000);

    this.ws.on('close', () => clearInterval(pingInterval));
  }

  private sendSubscription(tokenIds: string[]) {
    this.ws?.send(JSON.stringify({
      assets_ids: tokenIds,
      type: 'market',
      custom_feature_enabled: true,
    }));
    console.log(`PriceFeed subscribed to ${tokenIds.length} tokens (total: ${this.subscribedTokens.length})`);
  }
}
