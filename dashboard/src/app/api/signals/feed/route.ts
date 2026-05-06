import { NextResponse, type NextRequest } from "next/server";
import { getRecentSignals } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const limit = Math.min(parseInt(req.nextUrl.searchParams.get("limit") || "50", 10) || 50, 200);
  try {
    return NextResponse.json(getRecentSignals(limit));
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "signal feed failed" },
      { status: 500 }
    );
  }
}
