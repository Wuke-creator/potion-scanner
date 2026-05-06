"use client";

import { useEffect, useState } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Tabs } from "@/components/ui/Tabs";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/Table";
import { Badge } from "@/components/ui/Badge";
import { formatRelativeTime } from "@/lib/utils";
import type {
  ChannelStats,
  CallerStats,
  SignalFeedRow,
  PnLBucket,
  FunnelStage,
} from "@/lib/types";

type SubTab = "channel" | "caller" | "feed";

export function SignalsPanel() {
  const [tab, setTab] = useState<SubTab>("channel");
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between flex-wrap gap-3">
          <CardTitle className="text-lg text-zinc-100">Signal performance</CardTitle>
          <Tabs
            value={tab}
            onChange={(v) => setTab(v as SubTab)}
            options={[
              { value: "channel", label: "By channel" },
              { value: "caller", label: "By caller" },
              { value: "feed", label: "Recent" },
            ]}
          />
        </div>
      </CardHeader>
      <CardContent>
        {tab === "channel" && <ChannelTab />}
        {tab === "caller" && <CallerTab />}
        {tab === "feed" && <FeedTab />}
        <SharedCharts />
      </CardContent>
    </Card>
  );
}

function ChannelTab() {
  const [data, setData] = useState<ChannelStats[] | null>(null);
  useEffect(() => {
    fetch("/api/signals/channel").then((r) => r.json()).then(setData).catch(() => setData([]));
  }, []);
  if (data === null) return <Loading />;
  if (data.length === 0) return <Empty msg="No signals yet." />;
  return (
    <Table>
      <THead>
        <TR>
          <TH>Channel</TH>
          <TH className="text-right">7d</TH>
          <TH className="text-right">30d</TH>
          <TH className="text-right">Win rate</TH>
          <TH className="text-right">Avg PnL</TH>
          <TH>Best</TH>
          <TH>Worst</TH>
        </TR>
      </THead>
      <TBody>
        {data.map((c) => (
          <TR key={c.channel_key}>
            <TD className="font-mono text-xs">{c.channel_key}</TD>
            <TD className="text-right">{c.signal_count_7d}</TD>
            <TD className="text-right">{c.signal_count_30d}</TD>
            <TD className="text-right">
              {c.closed_count
                ? `${(c.win_rate * 100).toFixed(0)}% (${c.win_count}/${c.closed_count})`
                : "—"}
            </TD>
            <TD className="text-right">
              {c.closed_count ? `${c.avg_pnl_pct.toFixed(1)}%` : "—"}
            </TD>
            <TD className="text-emerald-400 text-xs">
              {c.best_pnl_pct !== null ? `${c.best_pnl_pct.toFixed(1)}% ${c.best_trade_pair ?? ""}` : "—"}
            </TD>
            <TD className="text-red-400 text-xs">
              {c.worst_pnl_pct !== null ? `${c.worst_pnl_pct.toFixed(1)}% ${c.worst_trade_pair ?? ""}` : "—"}
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function CallerTab() {
  const [data, setData] = useState<{ callers: CallerStats[]; legacy_untracked: number } | null>(null);
  useEffect(() => {
    fetch("/api/signals/caller").then((r) => r.json()).then(setData).catch(() => setData({ callers: [], legacy_untracked: 0 }));
  }, []);
  if (data === null) return <Loading />;
  return (
    <div className="space-y-3">
      {data.legacy_untracked > 0 && (
        <Card>
          <CardContent className="p-3 text-xs text-zinc-400">
            <span className="text-amber-300">{data.legacy_untracked.toLocaleString()}</span>{" "}
            legacy signals don't carry a caller ID (recorded before the per-caller migration).
            New signals from this point forward will populate this view.
          </CardContent>
        </Card>
      )}
      {data.callers.length === 0 ? (
        <Empty msg="No per-caller data yet. New signals after the bot upgrade will show here." />
      ) : (
        <Table>
          <THead>
            <TR>
              <TH>Caller (Discord ID)</TH>
              <TH className="text-right">7d</TH>
              <TH className="text-right">30d</TH>
              <TH className="text-right">Win rate</TH>
              <TH className="text-right">Avg PnL</TH>
            </TR>
          </THead>
          <TBody>
            {data.callers.map((c) => (
              <TR key={c.source_discord_user_id}>
                <TD className="font-mono text-xs">{c.source_discord_user_id}</TD>
                <TD className="text-right">{c.signal_count_7d}</TD>
                <TD className="text-right">{c.signal_count_30d}</TD>
                <TD className="text-right">
                  {c.closed_count
                    ? `${(c.win_rate * 100).toFixed(0)}% (${c.win_count}/${c.closed_count})`
                    : "—"}
                </TD>
                <TD className="text-right">
                  {c.closed_count ? `${c.avg_pnl_pct.toFixed(1)}%` : "—"}
                </TD>
              </TR>
            ))}
          </TBody>
        </Table>
      )}
    </div>
  );
}

function FeedTab() {
  const [data, setData] = useState<SignalFeedRow[] | null>(null);
  useEffect(() => {
    fetch("/api/signals/feed").then((r) => r.json()).then(setData).catch(() => setData([]));
  }, []);
  if (data === null) return <Loading />;
  if (data.length === 0) return <Empty msg="No signals." />;
  return (
    <Table>
      <THead>
        <TR>
          <TH className="w-28">When</TH>
          <TH>Pair</TH>
          <TH>Side</TH>
          <TH>Channel</TH>
          <TH>Lev</TH>
          <TH>Status</TH>
          <TH className="text-right">PnL</TH>
        </TR>
      </THead>
      <TBody>
        {data.map((r) => (
          <TR key={`${r.trade_id}-${r.channel_key}`}>
            <TD className="text-zinc-400">{formatRelativeTime(r.opened_at)}</TD>
            <TD className="font-mono">{r.pair}</TD>
            <TD>
              <Badge variant={r.side === "LONG" ? "success" : "destructive"}>{r.side}</Badge>
            </TD>
            <TD className="text-xs text-zinc-400 font-mono">{r.channel_key}</TD>
            <TD>{r.leverage}x</TD>
            <TD>
              {r.last_event_type ? (
                <Badge variant={statusVariant(r.last_event_type)}>{r.last_event_type}</Badge>
              ) : (
                <Badge>open</Badge>
              )}
            </TD>
            <TD className="text-right">
              {r.last_event_pnl !== null && r.last_event_pnl !== undefined
                ? `${r.last_event_pnl.toFixed(1)}%`
                : "—"}
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function SharedCharts() {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 mt-6">
      <FunnelChart />
      <PnLChart />
    </div>
  );
}

function FunnelChart() {
  const [data, setData] = useState<FunnelStage[] | null>(null);
  useEffect(() => {
    fetch("/api/signals/funnel").then((r) => r.json()).then(setData).catch(() => setData([]));
  }, []);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Lifecycle (30d)</CardTitle>
      </CardHeader>
      <CardContent>
        {data === null ? (
          <Loading />
        ) : (
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={data}>
              <XAxis dataKey="stage" stroke="#71717a" fontSize={11} />
              <YAxis stroke="#71717a" fontSize={11} />
              <Tooltip
                contentStyle={{
                  background: "#18181b",
                  border: "1px solid #27272a",
                  borderRadius: 8,
                }}
              />
              <Bar dataKey="count" fill="#3b82f6" />
            </BarChart>
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  );
}

function PnLChart() {
  const [data, setData] = useState<PnLBucket[] | null>(null);
  useEffect(() => {
    fetch("/api/signals/distribution").then((r) => r.json()).then(setData).catch(() => setData([]));
  }, []);
  return (
    <Card>
      <CardHeader>
        <CardTitle>PnL distribution (closed signals)</CardTitle>
      </CardHeader>
      <CardContent>
        {data === null ? (
          <Loading />
        ) : (
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={data}>
              <XAxis dataKey="range" stroke="#71717a" fontSize={11} />
              <YAxis stroke="#71717a" fontSize={11} />
              <Tooltip
                contentStyle={{
                  background: "#18181b",
                  border: "1px solid #27272a",
                  borderRadius: 8,
                }}
              />
              <Bar dataKey="count">
                {data.map((d, i) => (
                  <Cell
                    key={i}
                    fill={d.bucket_max <= 0 ? "#ef4444" : "#22c55e"}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  );
}

function statusVariant(eventType: string): "default" | "success" | "warn" | "destructive" {
  if (eventType === "all_tp_hit" || eventType === "tp_hit") return "success";
  if (eventType === "stop_hit") return "destructive";
  if (eventType === "breakeven") return "warn";
  return "default";
}

function Loading() {
  return <div className="text-sm text-zinc-500 py-8 text-center">Loading…</div>;
}

function Empty({ msg }: { msg: string }) {
  return <div className="text-sm text-zinc-500 py-8 text-center">{msg}</div>;
}
