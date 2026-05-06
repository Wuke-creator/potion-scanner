import { NextResponse } from "next/server";
import { getChannelStats } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(getChannelStats());
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "channel stats failed" },
      { status: 500 }
    );
  }
}
