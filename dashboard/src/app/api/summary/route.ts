import { NextResponse } from "next/server";
import { getSummary } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(getSummary());
  } catch (e) {
    const msg = e instanceof Error ? e.message : "summary failed";
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
