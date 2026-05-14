import { NextResponse } from "next/server";
import { getSuppressionLog } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(getSuppressionLog());
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "suppressions failed" },
      { status: 500 }
    );
  }
}
