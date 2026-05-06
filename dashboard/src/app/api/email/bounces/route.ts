import { NextResponse } from "next/server";
import { getHardBounces } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(getHardBounces());
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "bounces failed" },
      { status: 500 }
    );
  }
}
