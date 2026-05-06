import { NextResponse, type NextRequest } from "next/server";
import { ackLeadershipMention } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function POST(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const numId = Number(id);
  if (!Number.isFinite(numId)) {
    return NextResponse.json({ error: "bad id" }, { status: 400 });
  }
  try {
    ackLeadershipMention(numId);
    return NextResponse.json({ ok: true });
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "ack failed" },
      { status: 500 }
    );
  }
}
