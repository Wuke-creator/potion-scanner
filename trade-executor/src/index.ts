import express, { Request, Response } from "express";

import { submitDelegatedTrade } from "./ostium.js";
import { TradeRequestSchema } from "./types.js";

const PORT = Number(process.env.PORT ?? 3001);
const SHARED_SECRET = process.env.TRADE_EXECUTOR_SECRET ?? "";

if (!SHARED_SECRET) {
  console.error("[fatal] TRADE_EXECUTOR_SECRET env var is required");
  process.exit(1);
}

const app = express();
app.use(express.json({ limit: "32kb" }));

app.use((req, res, next) => {
  if (req.path === "/health") return next();
  const provided = req.header("x-executor-secret") ?? "";
  if (provided !== SHARED_SECRET) {
    return res.status(401).json({ error: "unauthorized" });
  }
  next();
});

app.get("/health", (_req: Request, res: Response) => {
  res.json({ ok: true, service: "potion-trade-executor" });
});

app.post("/trade", async (req: Request, res: Response) => {
  const parsed = TradeRequestSchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({
      error: parsed.error.issues.map((i) => i.message).join("; "),
      code: "validation_error",
    });
  }

  try {
    const result = await submitDelegatedTrade(parsed.data);
    return res.status(200).json(result);
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    const safe = message.replace(parsed.data.delegate_private_key, "[redacted]");
    console.error("[/trade] failed:", safe);
    return res.status(500).json({ error: safe, code: "executor_error" });
  }
});

app.listen(PORT, () => {
  console.log(`[trade-executor] listening on :${PORT}`);
});
