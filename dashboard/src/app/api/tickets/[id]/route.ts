import { NextResponse, type NextRequest } from "next/server";
import { getTicket, updateTicketStatus } from "@/lib/queries";
import type { TicketStatus } from "@/lib/types";

export const dynamic = "force-dynamic";

interface RouteCtx {
  params: Promise<{ id: string }>;
}

export async function GET(_req: NextRequest, { params }: RouteCtx) {
  const { id } = await params;
  const messageId = Number(id);
  if (!Number.isFinite(messageId)) {
    return NextResponse.json({ error: "bad id" }, { status: 400 });
  }
  const t = getTicket(messageId);
  if (!t) return NextResponse.json({ error: "not found" }, { status: 404 });
  return NextResponse.json(t);
}

export async function PATCH(req: NextRequest, { params }: RouteCtx) {
  const { id } = await params;
  const messageId = Number(id);
  if (!Number.isFinite(messageId)) {
    return NextResponse.json({ error: "bad id" }, { status: 400 });
  }
  const body = await req.json().catch(() => ({}));
  const status = body.status as TicketStatus | undefined;
  const notes = typeof body.notes === "string" ? body.notes : undefined;
  if (!status || !["open", "in_progress", "resolved"].includes(status)) {
    return NextResponse.json({ error: "bad status" }, { status: 400 });
  }
  const updated = updateTicketStatus(messageId, status, notes);
  if (!updated) return NextResponse.json({ error: "not found" }, { status: 404 });
  return NextResponse.json(updated);
}
