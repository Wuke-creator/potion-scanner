import { NextResponse } from "next/server";
import { getCallerStats, legacyUntrackedSignalCount } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json({
      callers: getCallerStats(),
      legacy_untracked: legacyUntrackedSignalCount(),
    });
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "caller stats failed" },
      { status: 500 }
    );
  }
}
