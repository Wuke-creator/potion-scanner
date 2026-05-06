"use client";

import { useEffect, useState } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Tabs } from "@/components/ui/Tabs";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/Table";
import { formatRelativeTime } from "@/lib/utils";
import type {
  QueueRow,
  EmailKpis,
  BroadcastRow,
  BounceRow,
  SequenceCell,
  ExitReasonRow,
} from "@/lib/types";

type SubTab = "kpis" | "queue" | "broadcasts" | "bounces" | "sequence" | "exit";

export function EmailPanel() {
  const [tab, setTab] = useState<SubTab>("kpis");
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between flex-wrap gap-3">
          <CardTitle className="text-lg text-zinc-100">Email pipeline</CardTitle>
          <Tabs
            value={tab}
            onChange={(v) => setTab(v as SubTab)}
            options={[
              { value: "kpis", label: "KPIs" },
              { value: "queue", label: "Queue" },
              { value: "broadcasts", label: "Broadcasts" },
              { value: "bounces", label: "Bounces" },
              { value: "sequence", label: "Sequences" },
              { value: "exit", label: "Exit reasons" },
            ]}
          />
        </div>
      </CardHeader>
      <CardContent>
        {tab === "kpis" && <KpisTab />}
        {tab === "queue" && <QueueTab />}
        {tab === "broadcasts" && <BroadcastsTab />}
        {tab === "bounces" && <BouncesTab />}
        {tab === "sequence" && <SequenceTab />}
        {tab === "exit" && <ExitTab />}
      </CardContent>
    </Card>
  );
}

function KpisTab() {
  const [k, setK] = useState<EmailKpis | null>(null);
  useEffect(() => {
    fetch("/api/email/kpis").then((r) => r.json()).then(setK).catch(() => setK(null));
  }, []);
  if (k === null) return <Loading />;
  const cells: { label: string; value: string }[] = [
    { label: "Sent", value: k.sent.toLocaleString() },
    { label: "Delivered", value: k.delivered.toLocaleString() },
    { label: "Opened", value: `${k.opened.toLocaleString()} (${(k.open_rate * 100).toFixed(1)}%)` },
    { label: "Clicked", value: `${k.clicked.toLocaleString()} (${(k.click_rate * 100).toFixed(1)}%)` },
    { label: "Bounced", value: `${k.bounced.toLocaleString()} (${(k.bounce_rate * 100).toFixed(2)}%)` },
    { label: "Complained", value: k.complained.toLocaleString() },
  ];
  return (
    <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
      {cells.map((c) => (
        <Card key={c.label}>
          <CardContent className="p-3">
            <div className="text-xs uppercase tracking-wider text-zinc-500">{c.label}</div>
            <div className="text-xl font-semibold mt-1">{c.value}</div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function QueueTab() {
  const [data, setData] = useState<QueueRow[] | null>(null);
  useEffect(() => {
    fetch("/api/email/queue").then((r) => r.json()).then(setData).catch(() => setData([]));
  }, []);
  if (data === null) return <Loading />;
  if (data.length === 0) return <Empty msg="Queue is empty." />;
  return (
    <ResponsiveContainer width="100%" height={Math.max(220, data.length * 32 + 40)}>
      <BarChart data={data} layout="vertical">
        <XAxis type="number" stroke="#71717a" fontSize={11} />
        <YAxis dataKey="sequence" type="category" stroke="#71717a" fontSize={11} width={140} />
        <Tooltip
          contentStyle={{
            background: "#18181b",
            border: "1px solid #27272a",
            borderRadius: 8,
          }}
        />
        <Bar dataKey="pending" fill="#3b82f6" />
      </BarChart>
    </ResponsiveContainer>
  );
}

function BroadcastsTab() {
  const [data, setData] = useState<BroadcastRow[] | null>(null);
  useEffect(() => {
    fetch("/api/email/broadcasts").then((r) => r.json()).then(setData).catch(() => setData([]));
  }, []);
  if (data === null) return <Loading />;
  if (data.length === 0) return <Empty msg="No broadcasts tagged yet." />;
  return (
    <Table>
      <THead>
        <TR>
          <TH>Broadcast</TH>
          <TH className="text-right">Delivered</TH>
          <TH className="text-right">Opened</TH>
          <TH className="text-right">Clicked</TH>
          <TH className="text-right">Bounced</TH>
          <TH className="text-right">Complained</TH>
        </TR>
      </THead>
      <TBody>
        {data.map((r) => (
          <TR key={r.broadcast_id}>
            <TD className="font-mono text-xs">{r.broadcast_id}</TD>
            <TD className="text-right">{r.delivered.toLocaleString()}</TD>
            <TD className="text-right">
              {r.opened.toLocaleString()}
              {r.delivered ? <span className="text-zinc-500 ml-1 text-xs">({((r.opened / r.delivered) * 100).toFixed(1)}%)</span> : null}
            </TD>
            <TD className="text-right">
              {r.clicked.toLocaleString()}
              {r.delivered ? <span className="text-zinc-500 ml-1 text-xs">({((r.clicked / r.delivered) * 100).toFixed(1)}%)</span> : null}
            </TD>
            <TD className="text-right">{r.bounced.toLocaleString()}</TD>
            <TD className="text-right">{r.complained.toLocaleString()}</TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function BouncesTab() {
  const [data, setData] = useState<BounceRow[] | null>(null);
  useEffect(() => {
    fetch("/api/email/bounces").then((r) => r.json()).then(setData).catch(() => setData([]));
  }, []);
  if (data === null) return <Loading />;
  if (data.length === 0) return <Empty msg="No hard bounces." />;
  return (
    <Table>
      <THead>
        <TR>
          <TH>Recipient</TH>
          <TH>Last bounce</TH>
          <TH>Reason</TH>
          <TH className="text-right">Count</TH>
        </TR>
      </THead>
      <TBody>
        {data.map((r) => (
          <TR key={r.recipient}>
            <TD className="font-mono text-xs">{r.recipient}</TD>
            <TD>{formatRelativeTime(r.bounced_at)}</TD>
            <TD className="text-zinc-400 text-xs">{r.bounce_message ?? "—"}</TD>
            <TD className="text-right">{r.count}</TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function SequenceTab() {
  const [data, setData] = useState<SequenceCell[] | null>(null);
  useEffect(() => {
    fetch("/api/email/sequence").then((r) => r.json()).then(setData).catch(() => setData([]));
  }, []);
  if (data === null) return <Loading />;
  if (data.length === 0) return <Empty msg="No sequence sends recorded yet." />;
  // Pivot data into a heatmap: rows = sequence, cols = day.
  const sequences = Array.from(new Set(data.map((d) => d.sequence))).sort();
  const days = Array.from(new Set(data.map((d) => d.day))).sort((a, b) => a - b);
  const cell = (seq: string, day: number): SequenceCell | undefined =>
    data.find((d) => d.sequence === seq && d.day === day);
  return (
    <Table>
      <THead>
        <TR>
          <TH>Sequence</TH>
          {days.map((d) => (
            <TH key={d} className="text-right">d{d}</TH>
          ))}
        </TR>
      </THead>
      <TBody>
        {sequences.map((s) => (
          <TR key={s}>
            <TD className="font-mono text-xs">{s}</TD>
            {days.map((d) => {
              const c = cell(s, d);
              if (!c || !c.sent) return <TD key={d} className="text-right text-zinc-600">—</TD>;
              const openRate = c.delivered ? (c.opened / c.delivered) * 100 : 0;
              return (
                <TD key={d} className="text-right">
                  <div className="text-xs">{c.sent}</div>
                  <div className="text-[10px] text-zinc-500">{openRate.toFixed(0)}% open</div>
                </TD>
              );
            })}
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function ExitTab() {
  const [data, setData] = useState<ExitReasonRow[] | null>(null);
  useEffect(() => {
    fetch("/api/email/exit-reasons").then((r) => r.json()).then(setData).catch(() => setData([]));
  }, []);
  if (data === null) return <Loading />;
  if (data.length === 0) return <Empty msg="No cancellation surveys yet." />;
  const total = data.reduce((sum, r) => sum + r.count, 0);
  return (
    <div className="space-y-2">
      {data.map((r) => {
        const pct = (r.count / total) * 100;
        return (
          <div key={r.exit_reason}>
            <div className="flex justify-between text-sm mb-1">
              <span>{r.exit_reason || "(no reason)"}</span>
              <span className="text-zinc-400">
                {r.count.toLocaleString()} ({pct.toFixed(1)}%)
              </span>
            </div>
            <div className="h-2 bg-zinc-800 rounded overflow-hidden">
              <div
                className="h-full bg-blue-500"
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Loading() {
  return <div className="text-sm text-zinc-500 py-8 text-center">Loading…</div>;
}

function Empty({ msg }: { msg: string }) {
  return <div className="text-sm text-zinc-500 py-8 text-center">{msg}</div>;
}
