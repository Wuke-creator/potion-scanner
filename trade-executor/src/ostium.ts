import { OstiumClient, OrderType } from "@ostium/builder-sdk";

import type { TradeRequest, TradeResponse } from "./types.js";

const PAIR_ID_CACHE = new Map<string, number>();
let PAIR_CACHE_TS = 0;
const PAIR_CACHE_TTL_MS = 5 * 60 * 1000;

let READ_CLIENT: OstiumClient | null = null;

async function getReadClient(): Promise<OstiumClient> {
  if (READ_CLIENT) return READ_CLIENT;
  const args: Parameters<typeof OstiumClient.createReadOnly>[0] = {};
  if (process.env.ARB_RPC_URL) args.rpcUrl = process.env.ARB_RPC_URL;
  READ_CLIENT = await OstiumClient.createReadOnly(args);
  return READ_CLIENT;
}

async function refreshPairCache(client: OstiumClient): Promise<void> {
  const { pairs } = await client.getPairs();
  PAIR_ID_CACHE.clear();
  for (const p of pairs) {
    PAIR_ID_CACHE.set((p.pairFrom || "").toUpperCase(), Number(p.pairId));
  }
  PAIR_CACHE_TS = Date.now();
}

async function resolvePairId(
  client: OstiumClient,
  base: string,
): Promise<number> {
  const key = base.toUpperCase();
  const now = Date.now();
  if (PAIR_ID_CACHE.size === 0 || now - PAIR_CACHE_TS > PAIR_CACHE_TTL_MS) {
    await refreshPairCache(client);
  }
  const id = PAIR_ID_CACHE.get(key);
  if (id === undefined) {
    throw new Error(`unknown pair: ${base}`);
  }
  return id;
}

/**
 * Return the list of base symbols Ostium currently lists as tradeable
 * markets (e.g. ["BTC", "ETH", "SOL", ...]). Uses a read-only client so
 * no delegate key is needed. Cached for PAIR_CACHE_TTL_MS; on a refresh
 * failure the last-known list is returned (callers treat an empty list
 * as "coverage unknown" and fail safe).
 */
export async function listSupportedSymbols(): Promise<string[]> {
  const now = Date.now();
  if (PAIR_ID_CACHE.size === 0 || now - PAIR_CACHE_TS > PAIR_CACHE_TTL_MS) {
    try {
      const client = await getReadClient();
      await refreshPairCache(client);
    } catch (err) {
      console.warn(
        "[ostium] listSupportedSymbols refresh failed, using cached:",
        err instanceof Error ? err.message : String(err),
      );
    }
  }
  return [...PAIR_ID_CACHE.keys()];
}

export async function submitDelegatedTrade(
  req: TradeRequest,
): Promise<TradeResponse> {
  const clientArgs: Parameters<typeof OstiumClient.createDelegatedAndGasless>[0] = {
    delegatePrivateKey: req.delegate_private_key as `0x${string}`,
    traderAddress: req.trader_address as `0x${string}`,
  };

  if (req.builder_address && req.builder_fee_bps > 0) {
    clientArgs.builder = {
      address: req.builder_address as `0x${string}`,
      feeBps: req.builder_fee_bps,
    };
  }

  if (process.env.ARB_RPC_URL) {
    clientArgs.rpcUrl = process.env.ARB_RPC_URL;
  }
  if (process.env.OSTIUM_PIMLICO_URL) {
    clientArgs.pimlicoUrl = process.env.OSTIUM_PIMLICO_URL;
  }
  if (process.env.OSTIUM_SPONSORSHIP_POLICY_ID) {
    clientArgs.sponsorshipPolicyId = process.env.OSTIUM_SPONSORSHIP_POLICY_ID;
  }

  const client = await OstiumClient.createDelegatedAndGasless(clientArgs);
  const pairId = await resolvePairId(client, req.pair_base);

  const orderType =
    req.order_type === "LIMIT" ? OrderType.Limit : OrderType.Market;
  // For market orders the SDK uses live mid-price; pass "0" as a sentinel.
  // For limit orders, use the user-supplied limit price.
  const price =
    req.order_type === "LIMIT" && req.limit_price !== null
      ? String(req.limit_price)
      : "0";

  const result = await client.openTrade({
    pairId,
    buy: req.direction === "LONG",
    price,
    collateral: String(req.collateral_usdc),
    leverage: String(req.leverage),
    type: orderType,
    slippage: req.slippage_bps,
    takeProfit: req.take_profit !== null ? String(req.take_profit) : undefined,
    stopLoss: req.stop_loss !== null ? String(req.stop_loss) : undefined,
  });

  return {
    tx_hash: result.txHash,
    pair_id: pairId,
    status: "submitted",
  };
}
