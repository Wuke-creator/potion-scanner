import { NextResponse } from "next/server";
import { getEngagementSegments } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(getEngagementSegments());
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "engagement failed" },
      { status: 500 }
    );
  }
}
