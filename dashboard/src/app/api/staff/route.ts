import { NextResponse } from "next/server";
import { getStaffPerformance } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(getStaffPerformance());
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "staff failed" },
      { status: 500 }
    );
  }
}
