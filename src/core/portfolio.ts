import { config } from '../config.js';

export interface Position {
  token_id: string;
  question: string;
  direction: string;
  num_shares: number;
  entry_price: number;
  cur_price: number;
  total_cost: number;
  current_value: number;
  pnl: number;
  category: string;
}

/**
 * Fetch live positions from Polymarket Data API.
 * No proxy needed — public endpoint.
 */
export async function fetchPositions(): Promise<Position[]> {
  if (!config.proxyWalletAddress) return [];

  try {
    const resp = await fetch(
      `${config.dataApi}/positions?user=${config.proxyWalletAddress}&sizeThreshold=0`,
      { headers: { 'User-Agent': 'Mozilla/5.0' } }
    );
    if (!resp.ok) return [];
    const data = await resp.json() as any[];

    return data.map(p => ({
      token_id: p.asset ?? '',
      question: p.title ?? '',
      direction: (p.outcome ?? '').toLowerCase(),
      num_shares: parseFloat(p.size ?? '0'),
      entry_price: parseFloat(p.avgPrice ?? '0'),
      cur_price: parseFloat(p.curPrice ?? '0'),
      total_cost: parseFloat(p.initialValue ?? '0'),
      current_value: parseFloat(p.currentValue ?? '0'),
      pnl: parseFloat(p.cashPnl ?? '0'),
      category: p.eventSlug ?? '',
    }));
  } catch (e: any) {
    console.warn(`Position fetch error: ${e.message}`);
    return [];
  }
}

/**
 * Fetch portfolio value from Polymarket Data API.
 */
export async function fetchPortfolioValue(): Promise<number> {
  if (!config.proxyWalletAddress) return 0;

  try {
    const resp = await fetch(
      `${config.dataApi}/value?user=${config.proxyWalletAddress}`,
      { headers: { 'User-Agent': 'Mozilla/5.0' } }
    );
    if (!resp.ok) return 0;
    const data = await resp.json() as any[];
    return data.length > 0 ? parseFloat(data[0].value ?? '0') : 0;
  } catch {
    return 0;
  }
}
