import { NextResponse } from "next/server";
import { getEmailKpis } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(getEmailKpis());
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "kpis failed" },
      { status: 500 }
    );
  }
}
