import { ClobClient, Side } from '@polymarket/clob-client';
import { createWalletClient, http } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { polygon } from 'viem/chains';
import { config } from '../config.js';
import { query } from '../db/client.js';

/**
 * OrderManager — single entry point for ALL buys and sells.
 *
 * Node.js is single-threaded: the owned set check + order placement
 * happens synchronously on the event loop. No race conditions possible.
 */

let clobClient: ClobClient | null = null;

function getClient(): ClobClient {
  if (!clobClient) {
    clobClient = new ClobClient(
      config.clobApi,
      137, // Polygon chain ID
      config.privateKey ? createWalletClient({
        account: privateKeyToAccount(config.privateKey as `0x${string}`),
        chain: polygon,
        transport: http(),
      }) : undefined,
      {
        key: config.polyApiKey,
        secret: config.polyApiSecret,
        passphrase: config.polyApiPassphrase,
      },
      2, // signature type (proxy wallet)
      config.proxyWalletAddress,
    );
    console.log('CLOB client initialized');
  }
  return clobClient;
}

// The set of token IDs we own or have pending orders for.
// Checked SYNCHRONOUSLY before any buy. Single-threaded = no races.
const ownedTokens = new Set<string>();

export interface BuyResult {
  success: boolean;
  orderId?: string;
  tpOrderId?: string;
  shares: number;
  price: number;
  error?: string;
}

export interface SellResult {
  success: boolean;
  orderId?: string;
  error?: string;
}

/**
 * Load existing positions into ownedTokens on startup.
 */
export async function loadOwnedTokens(positions: Array<{ token_id: string }>) {
  for (const p of positions) {
    if (p.token_id) ownedTokens.add(p.token_id);
  }
  console.log(`OrderManager: loaded ${ownedTokens.size} owned tokens`);
}

/**
 * Check if a token is already owned/pending.
 */
export function isOwned(tokenId: string): boolean {
  return ownedTokens.has(tokenId);
}

/**
 * Buy shares of a token. Places limit buy + TP sell.
 * Returns immediately if token already owned.
 */
export async function buy(
  tokenId: string,
  price: number,
  shares: number,
  question: string,
  adapter: string,
  tpPercent: number,
): Promise<BuyResult> {
  // Synchronous check — no race condition in single-threaded Node
  if (ownedTokens.has(tokenId)) {
    return { success: false, shares: 0, price, error: 'already_owned' };
  }

  if (!config.tradingEnabled) {
    return { success: false, shares: 0, price, error: 'trading_disabled' };
  }

  if (shares < 5) {
    return { success: false, shares: 0, price, error: 'min_5_shares' };
  }

  // Mark as owned BEFORE placing order
  ownedTokens.add(tokenId);

  try {
    const client = getClient();

    // Limit buy (maker = 0% fee)
    const buyOrder = await client.createAndPostOrder({
      tokenID: tokenId,
      price,
      size: shares,
      side: Side.BUY,
    });

    if (!buyOrder?.success) {
      ownedTokens.delete(tokenId);
      return { success: false, shares: 0, price, error: buyOrder?.errorMsg || 'buy_failed' };
    }

    console.log(`BUY: ${question.slice(0, 40)} ${shares} shares @ $${price.toFixed(3)} [${adapter}]`);

    // Place TP sell immediately (maker = 0% fee)
    const tpPrice = Math.round(price * (1 + tpPercent) * 100) / 100;
    let tpOrderId: string | undefined;

    try {
      const tpOrder = await client.createAndPostOrder({
        tokenID: tokenId,
        price: tpPrice,
        size: shares,
        side: Side.SELL,
      });
      tpOrderId = tpOrder?.orderID;
      console.log(`  TP sell at $${tpPrice.toFixed(3)} (${(tpPercent * 100).toFixed(0)}%)`);
    } catch (e: any) {
      console.warn(`  TP sell failed: ${e.message}`);
    }

    // Record in DB
    try {
      await query(
        `INSERT INTO trades (token_id, question, direction, shares, entry_price, cost, status, adapter, tp_order_id)
         VALUES ($1, $2, 'buy', $3, $4, $5, 'open', $6, $7)`,
        [tokenId, question, shares, price, price * shares, adapter, tpOrderId]
      );
    } catch (e: any) {
      console.warn(`DB trade save failed: ${e.message}`);
    }

    return { success: true, orderId: buyOrder.orderID, tpOrderId, shares, price };

  } catch (e: any) {
    ownedTokens.delete(tokenId);
    console.error(`Buy error: ${e.message}`);
    return { success: false, shares: 0, price, error: e.message };
  }
}

/**
 * Sell shares of a token. For manual close or stop loss.
 */
export async function sell(
  tokenId: string,
  price: number,
  shares: number,
  reason: string,
): Promise<SellResult> {
  if (shares < 5) {
    return { success: false, error: 'min_5_shares' };
  }

  try {
    const client = getClient();

    const order = await client.createAndPostOrder({
      tokenID: tokenId,
      price,
      size: Math.floor(shares),
      side: Side.SELL,
    });

    if (order?.success) {
      ownedTokens.delete(tokenId);
      console.log(`SELL: ${reason} ${Math.floor(shares)} shares @ $${price.toFixed(3)}`);

      // Update trade in DB
      try {
        await query(
          `UPDATE trades SET status = 'closed', exit_price = $1, revenue = $2,
           pnl = $2 - cost, closed_at = NOW() WHERE token_id = $3 AND status = 'open'`,
          [price, price * shares, tokenId]
        );
      } catch {}

      return { success: true, orderId: order.orderID };
    }

    return { success: false, error: order?.errorMsg || 'sell_failed' };
  } catch (e: any) {
    console.error(`Sell error: ${e.message}`);
    return { success: false, error: e.message };
  }
}

/**
 * Cancel all open orders.
 */
export async function cancelAll(): Promise<void> {
  try {
    const client = getClient();
    await client.cancelAll();
    console.log('All orders cancelled');
  } catch (e: any) {
    console.error(`Cancel all error: ${e.message}`);
  }
}

/**
 * Remove a token from owned set (e.g. when position disappears from Polymarket).
 */
export function releaseToken(tokenId: string) {
  ownedTokens.delete(tokenId);
}

export function getOwnedCount(): number {
  return ownedTokens.size;
}
