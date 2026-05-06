import { NextResponse } from "next/server";
import { getBroadcasts } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(getBroadcasts());
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "broadcasts failed" },
      { status: 500 }
    );
  }
}
