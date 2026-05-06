import { NextResponse } from "next/server";
import { getSequenceHeatmap } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(getSequenceHeatmap());
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "sequence failed" },
      { status: 500 }
    );
  }
}
