import { NextResponse } from "next/server";
import { getFunnelStages } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(getFunnelStages());
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "funnel failed" },
      { status: 500 }
    );
  }
}
