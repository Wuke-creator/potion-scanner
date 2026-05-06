import { NextResponse } from "next/server";
import { getEmailQueue } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(getEmailQueue());
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "queue failed" },
      { status: 500 }
    );
  }
}
