import { NextResponse, type NextRequest } from "next/server";
import { listLeadershipMentions } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  const ackParam = sp.get("acknowledged");
  const acknowledged = ackParam === null ? undefined : ackParam === "true";
  const mentionedId = sp.get("mentionedId") || undefined;
  const limit = Math.min(parseInt(sp.get("limit") || "100", 10) || 100, 500);
  try {
    return NextResponse.json(listLeadershipMentions({ acknowledged, mentionedId, limit }));
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "leadership failed" },
      { status: 500 }
    );
  }
}
