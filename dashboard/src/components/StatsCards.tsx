"use client";

import { Card, CardContent } from "@/components/ui/Card";
import { Sparkline } from "@/components/Sparkline";
import { cn, formatNumber } from "@/lib/utils";
import type { SummaryCounts } from "@/lib/types";
import type { ReactNode } from "react";

interface StatsCardsProps {
  data: SummaryCounts | null;
  active: string | null;
  onSelect: (key: string) => void;
}

interface CardSpec {
  key: string;
  title: string;
  value: ReactNode;
  delta?: string;
  spark?: number[];
  tone?: "default" | "warn" | "danger" | "success";
  status?: "online" | "stale" | "offline" | null;
  hint?: string;
}

function emailStatus(lastActivityAt: number | null): { status: "online" | "stale" | "offline"; label: string } {
  if (!lastActivityAt) return { status: "offline", label: "no activity" };
  const ageSec = Math.floor(Date.now() / 1000) - lastActivityAt;
  if (ageSec < 6 * 3600) return { status: "online", label: relAge(ageSec) };
  if (ageSec < 24 * 3600) return { status: "stale", label: relAge(ageSec) };
  return { status: "offline", label: relAge(ageSec) };
}

function relAge(sec: number): string {
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

export function StatsCards({ data, active, onSelect }: StatsCardsProps) {
  const email = data ? emailStatus(data.lastEmailActivityAt) : null;
  const cards: CardSpec[] = [
    {
      key: "tickets",
      title: "Open complaints",
      value: data ? formatNumber(data.openComplaints) : "—",
      hint: data
        ? `${data.openComplaintsHigh} high severity · ${data.openTickets} support threads`
        : undefined,
      spark: data?.complaintsTrend14d,
      tone: data && data.openComplaintsHigh > 0 ? "danger" : data && data.openComplaints > 0 ? "warn" : "default",
    },
    {
      key: "leadership",
      title: "Leadership pings",
      value: data ? formatNumber(data.unackedLeadership) : "—",
      tone: data && data.unackedLeadership > 0 ? "warn" : "default",
    },
    {
      key: "signals",
      title: "Missed calls (7d)",
      value: data ? formatNumber(data.missedCalls7d) : "—",
      hint: data
        ? `${data.missedCallsBreakdown.stop_hit} SL · ${data.missedCallsBreakdown.canceled} cancel · ${data.missedCallsBreakdown.open_caller_tickets} caller-tickets`
        : undefined,
      spark: data?.missedCallsTrend14d,
      tone: data && data.missedCalls7d > 0 ? "warn" : "default",
    },
    {
      key: "signals",
      title: "Signals today",
      value: data ? formatNumber(data.signalsToday) : "—",
      delta: data ? deltaLabel(data.signalsToday, data.signalsYesterday) : undefined,
    },
    {
      key: "email",
      title: "Email pipeline",
      value: data ? formatNumber(data.emailQueueDepth) : "—",
      hint: email ? `${email.status === "online" ? "Online" : email.status === "stale" ? "Stale" : "Offline"} · last activity ${email.label}` : undefined,
      status: email?.status ?? null,
    },
    {
      key: "staff",
      title: "Verified Elite",
      value: data ? formatNumber(data.verifiedActive) : "—",
    },
  ];
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
      {cards.map((c, idx) => (
        <button
          key={`${c.key}-${idx}`}
          onClick={() => onSelect(c.key)}
          className="text-left"
        >
          <Card
            className={cn(
              "transition-colors",
              active === c.key ? "ring-1 ring-blue-500/60" : "hover:bg-zinc-900"
            )}
          >
            <CardContent className="p-3">
              <div className="flex items-start justify-between gap-2">
                <div className="text-xs uppercase tracking-wider text-zinc-500">
                  {c.title}
                </div>
                {c.status && <StatusDot status={c.status} />}
              </div>
              <div
                className={cn(
                  "text-2xl font-semibold mt-1",
                  c.tone === "warn" && "text-amber-300",
                  c.tone === "danger" && "text-red-300",
                  c.tone === "success" && "text-emerald-300"
                )}
              >
                {c.value}
              </div>
              {c.delta && (
                <div className="text-xs text-zinc-500 mt-0.5">{c.delta}</div>
              )}
              {c.hint && (
                <div className="text-[11px] text-zinc-500 mt-0.5 leading-tight">{c.hint}</div>
              )}
              {c.spark && c.spark.length >= 2 && (
                <div className="mt-2 -mx-1">
                  <Sparkline
                    data={c.spark}
                    height={24}
                    color={c.tone === "warn" ? "#f59e0b" : "#3b82f6"}
                  />
                </div>
              )}
            </CardContent>
          </Card>
        </button>
      ))}
    </div>
  );
}

function StatusDot({ status }: { status: "online" | "stale" | "offline" }) {
  const color =
    status === "online" ? "bg-emerald-400" : status === "stale" ? "bg-amber-400" : "bg-red-400";
  const ring =
    status === "online" ? "ring-emerald-400/40" : status === "stale" ? "ring-amber-400/40" : "ring-red-400/40";
  return (
    <span
      className={cn("inline-block w-2 h-2 rounded-full ring-2 mt-1", color, ring, status === "online" && "animate-pulse")}
      title={status}
    />
  );
}

function deltaLabel(today: number, yesterday: number): string {
  if (yesterday === 0 && today === 0) return "no change";
  if (yesterday === 0) return `+${today} vs 0 yesterday`;
  const pct = ((today - yesterday) / yesterday) * 100;
  const sign = pct >= 0 ? "+" : "";
  return `${sign}${pct.toFixed(0)}% vs yesterday`;
}
