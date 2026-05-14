import { NextRequest, NextResponse } from "next/server";
import { getUnsubscribes } from "@/lib/queries";

export const dynamic = "force-dynamic";

function readWindow(req: NextRequest): number {
  const raw = req.nextUrl.searchParams.get("days");
  const n = raw ? parseInt(raw, 10) : NaN;
  if (Number.isFinite(n) && n > 0 && n <= 365) return n;
  return 30;
}

export async function GET(req: NextRequest) {
  try {
    return NextResponse.json(getUnsubscribes(readWindow(req)));
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "unsubscribes failed" },
      { status: 500 }
    );
  }
}
