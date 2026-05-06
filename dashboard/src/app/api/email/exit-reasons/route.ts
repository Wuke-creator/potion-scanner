import { NextResponse } from "next/server";
import { getExitReasons } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(getExitReasons());
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "exit reasons failed" },
      { status: 500 }
    );
  }
}
