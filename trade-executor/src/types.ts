import { z } from "zod";

export const TradeRequestSchema = z.object({
  trader_address: z.string().regex(/^0x[a-fA-F0-9]{40}$/, "invalid trader_address"),
  delegate_private_key: z.string().regex(/^0x[a-fA-F0-9]{64}$/, "invalid delegate_private_key"),
  builder_address: z
    .string()
    .regex(/^0x[a-fA-F0-9]{40}$/, "invalid builder_address")
    .nullable(),
  builder_fee_bps: z.number().int().min(0).max(50),
  pair_base: z.string().min(1).max(16),
  direction: z.enum(["LONG", "SHORT"]),
  leverage: z.number().int().min(1).max(500),
  collateral_usdc: z.number().positive().max(1_000_000),
  slippage_bps: z.number().int().min(1).max(1000),
  take_profit: z.number().positive().nullable(),
  stop_loss: z.number().positive().nullable(),
  order_type: z.enum(["MARKET", "LIMIT"]).default("MARKET"),
  limit_price: z.number().positive().nullable().default(null),
});

export type TradeRequest = z.infer<typeof TradeRequestSchema>;

export interface TradeResponse {
  tx_hash: string;
  pair_id: number;
  status: "submitted";
}

export interface ErrorResponse {
  error: string;
  code?: string;
}
