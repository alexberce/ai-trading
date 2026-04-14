import { config } from '../config.js';
import { Market } from '../adapters/base.js';

/**
 * Gamma API feed — discovers markets and their metadata.
 * Polls periodically (not real-time).
 */
export class GammaFeed {
  private markets = new Map<string, Market>();

  /**
   * Fetch active events sorted by volume (most active first).
   * Returns all markets from those events.
   */
  async fetchActiveMarkets(): Promise<Market[]> {
    const resp = await fetch(
      `${config.gammaApi}/events?active=true&closed=false&limit=200&order=volume24hr&ascending=false`,
      { headers: { 'User-Agent': 'Mozilla/5.0' } }
    );

    if (!resp.ok) return [];

    const events = await resp.json() as any[];
    const now = new Date();
    const results: Market[] = [];

    for (const event of events) {
      const eventSlug = event.slug ?? '';

      for (const m of event.markets ?? []) {
        const prices = typeof m.outcomePrices === 'string'
          ? JSON.parse(m.outcomePrices) : (m.outcomePrices ?? []);
        const tokenIds = typeof m.clobTokenIds === 'string'
          ? JSON.parse(m.clobTokenIds) : (m.clobTokenIds ?? []);

        if (!tokenIds.length || tokenIds.length < 2) continue;

        const yesPrice = parseFloat(prices[0] ?? '0');
        const noPrice = parseFloat(prices[1] ?? '0');

        let hoursLeft: number | null = null;
        const endDate = m.endDateIso ?? '';
        if (endDate) {
          try {
            const end = new Date(endDate.length === 10 ? endDate + 'T23:59:59Z' : endDate);
            hoursLeft = (end.getTime() - now.getTime()) / 3600000;
          } catch {}
        }

        const market: Market = {
          marketId: m.id ?? '',
          question: m.question ?? '',
          category: this.categorize(eventSlug, m.question ?? ''),
          slug: eventSlug,
          yesPrice,
          noPrice,
          yesToken: tokenIds[0],
          noToken: tokenIds[1] ?? '',
          liquidity: parseFloat(m.liquidity ?? '0'),
          volume24h: parseFloat(m.volume24hr ?? '0'),
          endDate,
          hoursLeft,
        };

        this.markets.set(market.marketId, market);
        results.push(market);
      }
    }

    console.log(`GammaFeed: fetched ${results.length} markets from ${events.length} events`);
    return results;
  }

  getMarket(marketId: string): Market | undefined {
    return this.markets.get(marketId);
  }

  /**
   * Categorize a market based on its slug and question.
   */
  private categorize(slug: string, question: string): string {
    const s = slug.toLowerCase();
    const q = question.toLowerCase();

    // Sports
    if (/^(mlb|nhl|nba|nfl|epl|ucl|mls)-/.test(s)) return s.split('-')[0];

    // Esports
    if (/^(cs2|lol|dota|valorant|cblol)-/.test(s) || q.includes('counter-strike') || q.includes('lol:')) return 'esports';

    // Crypto
    if (q.includes('bitcoin') || q.includes('ethereum') || q.includes('solana') ||
        q.includes('btc') || q.includes('eth') || s.includes('btc-') || s.includes('eth-') ||
        s.includes('sol-') || s.includes('doge-') || s.includes('bnb-') ||
        s.includes('updown') || s.includes('crypto')) return 'crypto';

    // Politics
    if (q.includes('president') || q.includes('election') || q.includes('trump') ||
        q.includes('democrat') || q.includes('republican')) return 'politics';

    return 'other';
  }
}
