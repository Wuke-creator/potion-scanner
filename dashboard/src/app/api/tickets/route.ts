import { NextResponse, type NextRequest } from "next/server";
import { listTickets } from "@/lib/queries";
import type { TicketStatus } from "@/lib/types";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  const status = (sp.get("status") || "open") as TicketStatus | "all";
  const source = (sp.get("source") || "all") as "ticket" | "general" | "alpha" | "all";
  const mode = (sp.get("mode") || "complaints") as "complaints" | "all";
  const minSeverity = sp.get("minSeverity") as "low" | "medium" | "high" | null;
  const limit = Math.min(parseInt(sp.get("limit") || "200", 10) || 200, 1000);
  try {
    return NextResponse.json(
      listTickets({
        status,
        source,
        mode,
        minSeverity: minSeverity ?? undefined,
        limit,
      })
    );
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "tickets failed" },
      { status: 500 }
    );
  }
}
