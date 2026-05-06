import { NextResponse } from "next/server";
import { getPnLDistribution } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(getPnLDistribution());
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "distribution failed" },
      { status: 500 }
    );
  }
}
