"use client";

import { useEffect, useMemo, useState } from "react";
import {
  AreaChart,
  Area,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Legend,
  CartesianGrid,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Tabs } from "@/components/ui/Tabs";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/Table";
import { formatRelativeTime } from "@/lib/utils";
import type {
  QueueRow,
  EmailKpis,
  BroadcastRowV2,
  BounceRow,
  SequenceCell,
  ExitReasonRow,
  DeliverabilityBucket,
  DomainRow,
  LinkRow,
  SendTimeReport,
  EngagementSegment,
  SoftBounceRow,
  SuppressionLogRow,
  UnsubscribeReport,
} from "@/lib/types";

type SubTab =
  | "kpis"
  | "deliverability"
  | "domains"
  | "links"
  | "sendtimes"
  | "engagement"
  | "queue"
  | "broadcasts"
  | "bounces"
  | "unsubs"
  | "sequence"
  | "exit";

type Window = 7 | 30 | 90;

export function EmailPanel() {
  const [tab, setTab] = useState<SubTab>("kpis");
  const [windowDays, setWindowDays] = useState<Window>(30);
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between flex-wrap gap-3">
          <CardTitle className="text-lg text-zinc-100">Email pipeline</CardTitle>
          <div className="flex items-center gap-3 flex-wrap">
            <Tabs
              value={String(windowDays)}
              onChange={(v) => setWindowDays(Number(v) as Window)}
              options={[
                { value: "7", label: "7d" },
                { value: "30", label: "30d" },
                { value: "90", label: "90d" },
              ]}
            />
            <Tabs
              value={tab}
              onChange={(v) => setTab(v as SubTab)}
              options={[
                { value: "kpis", label: "KPIs" },
                { value: "deliverability", label: "Deliverability" },
                { value: "domains", label: "Inbox placement" },
                { value: "links", label: "Links" },
                { value: "sendtimes", label: "Send time" },
                { value: "engagement", label: "Engagement" },
                { value: "broadcasts", label: "Broadcasts" },
                { value: "bounces", label: "Bounces" },
                { value: "unsubs", label: "Unsubs" },
                { value: "queue", label: "Queue" },
                { value: "sequence", label: "Sequences" },
                { value: "exit", label: "Exit reasons" },
              ]}
            />
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {tab === "kpis" && <KpisTab windowDays={windowDays} />}
        {tab === "deliverability" && <DeliverabilityTab windowDays={windowDays} />}
        {tab === "domains" && <DomainsTab windowDays={windowDays} />}
        {tab === "links" && <LinksTab windowDays={windowDays} />}
        {tab === "sendtimes" && <SendTimesTab windowDays={windowDays} />}
        {tab === "engagement" && <EngagementTab />}
        {tab === "broadcasts" && <BroadcastsTab />}
        {tab === "bounces" && <BouncesTab windowDays={windowDays} />}
        {tab === "unsubs" && <UnsubsTab windowDays={windowDays} />}
        {tab === "queue" && <QueueTab />}
        {tab === "sequence" && <SequenceTab />}
        {tab === "exit" && <ExitTab />}
      </CardContent>
    </Card>
  );
}

// ─── KPIs (industry-standard rates with thresholds) ────────────────────────

interface Threshold {
  warn: number;   // crosses into yellow
  bad: number;    // crosses into red
  inverted?: boolean; // if true, lower is worse (e.g. open rate)
}

function colorFor(value: number, t: Threshold): string {
  const { warn, bad, inverted = false } = t;
  if (inverted) {
    if (value < bad) return "text-rose-400";
    if (value < warn) return "text-amber-400";
    return "text-emerald-400";
  }
  if (value > bad) return "text-rose-400";
  if (value > warn) return "text-amber-400";
  return "text-emerald-400";
}

function pct(v: number, digits = 1): string {
  return `${(v * 100).toFixed(digits)}%`;
}

function KpisTab({ windowDays }: { windowDays: Window }) {
  const [k, setK] = useState<EmailKpis | null>(null);
  useEffect(() => {
    setK(null);
    fetch(`/api/email/kpis?days=${windowDays}`)
      .then((r) => r.json())
      .then(setK)
      .catch(() => setK(null));
  }, [windowDays]);
  if (k === null) return <Loading />;

  const cards: {
    label: string;
    value: string;
    sub?: string;
    color?: string;
  }[] = [
    {
      label: "Sent",
      value: k.sent.toLocaleString(),
      sub: `${k.delivered.toLocaleString()} delivered`,
    },
    {
      label: "Delivery rate",
      value: pct(k.delivery_rate),
      sub: `${k.failed} failed`,
      color: colorFor(k.delivery_rate, { warn: 0.97, bad: 0.95, inverted: true }),
    },
    {
      label: "Open rate (unique)",
      value: pct(k.open_rate),
      sub: `${k.unique_opened.toLocaleString()} of ${k.delivered.toLocaleString()}`,
      color: colorFor(k.open_rate, { warn: 0.20, bad: 0.15, inverted: true }),
    },
    {
      label: "Click rate (unique)",
      value: pct(k.click_rate),
      sub: `${k.unique_clicked.toLocaleString()} of ${k.delivered.toLocaleString()}`,
      color: colorFor(k.click_rate, { warn: 0.025, bad: 0.02, inverted: true }),
    },
    {
      label: "CTOR",
      value: pct(k.ctor),
      sub: "click-to-open",
      color: colorFor(k.ctor, { warn: 0.10, bad: 0.07, inverted: true }),
    },
    {
      label: "Bounce rate",
      value: pct(k.bounce_rate, 2),
      sub: `${k.hard_bounced} hard / ${k.soft_bounced} soft`,
      color: colorFor(k.bounce_rate, { warn: 0.01, bad: 0.02 }),
    },
    {
      label: "Hard bounce rate",
      value: pct(k.hard_bounce_rate, 2),
      sub: "sender reputation risk",
      color: colorFor(k.hard_bounce_rate, { warn: 0.003, bad: 0.005 }),
    },
    {
      label: "Complaint rate",
      value: pct(k.complaint_rate, 3),
      sub: `${k.complained} complaints`,
      color: colorFor(k.complaint_rate, { warn: 0.0005, bad: 0.001 }),
    },
    {
      label: "Unsubscribe rate",
      value: pct(k.unsubscribe_rate, 2),
      sub: `${k.unsubscribed} unsubs`,
      color: colorFor(k.unsubscribe_rate, { warn: 0.003, bad: 0.005 }),
    },
  ];

  return (
    <div>
      <Card className="mb-3 border-zinc-700">
        <CardContent className="p-4 flex items-baseline justify-between flex-wrap gap-2">
          <div>
            <div className="text-xs uppercase tracking-wider text-zinc-500">
              Sent, last 24 hours
            </div>
            <div className="text-3xl font-semibold mt-1 text-zinc-100">
              {k.sent_24h.toLocaleString()}
            </div>
          </div>
          <div className="text-xs text-zinc-500 max-w-[260px] text-right">
            Rolling 24h send volume, independent of the {windowDays}d window
            above. Watch this against your Resend plan's daily send cap.
          </div>
        </CardContent>
      </Card>
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        {cards.map((c) => (
          <Card key={c.label}>
            <CardContent className="p-3">
              <div className="text-xs uppercase tracking-wider text-zinc-500">{c.label}</div>
              <div className={`text-xl font-semibold mt-1 ${c.color ?? ""}`}>{c.value}</div>
              {c.sub && (
                <div className="text-xs text-zinc-500 mt-1">{c.sub}</div>
              )}
            </CardContent>
          </Card>
        ))}
      </div>
      <p className="text-xs text-zinc-600 mt-4">
        Color: green = healthy, amber = watch, red = action needed. Thresholds are industry standard for B2C lifecycle email.
      </p>
    </div>
  );
}

// ─── Deliverability trend (stacked area) ──────────────────────────────────

function DeliverabilityTab({ windowDays }: { windowDays: Window }) {
  const [data, setData] = useState<DeliverabilityBucket[] | null>(null);
  useEffect(() => {
    setData(null);
    fetch(`/api/email/deliverability?days=${windowDays}`)
      .then((r) => r.json())
      .then(setData)
      .catch(() => setData([]));
  }, [windowDays]);
  if (data === null) return <Loading />;
  if (data.length === 0) return <Empty msg="No events recorded yet. Webhook armed?" />;

  return (
    <ResponsiveContainer width="100%" height={320}>
      <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
        <CartesianGrid stroke="#27272a" strokeDasharray="3 3" />
        <XAxis dataKey="day" stroke="#71717a" fontSize={11} />
        <YAxis stroke="#71717a" fontSize={11} />
        <Tooltip
          contentStyle={{
            background: "#18181b",
            border: "1px solid #27272a",
            borderRadius: 8,
          }}
        />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Area type="monotone" dataKey="delivered" stackId="1" stroke="#10b981" fill="#10b98180" name="delivered" />
        <Area type="monotone" dataKey="bounced" stackId="1" stroke="#ef4444" fill="#ef444480" name="bounced" />
        <Area type="monotone" dataKey="complained" stackId="1" stroke="#f97316" fill="#f9731680" name="complained" />
        <Area type="monotone" dataKey="delivery_delayed" stackId="1" stroke="#eab308" fill="#eab30880" name="delayed" />
        <Area type="monotone" dataKey="failed" stackId="1" stroke="#a3a3a3" fill="#a3a3a380" name="failed" />
      </AreaChart>
    </ResponsiveContainer>
  );
}

// ─── Per-domain inbox placement ───────────────────────────────────────────

function DomainsTab({ windowDays }: { windowDays: Window }) {
  const [data, setData] = useState<DomainRow[] | null>(null);
  useEffect(() => {
    setData(null);
    fetch(`/api/email/by-domain?days=${windowDays}`)
      .then((r) => r.json())
      .then(setData)
      .catch(() => setData([]));
  }, [windowDays]);

  // Average open rate across the visible domains, used to highlight outliers.
  // useMemo must run on every render (Rules of Hooks) — keep it ABOVE any
  // conditional return.
  const avgOpen = useMemo(
    () =>
      data && data.length > 0
        ? data.reduce((s, r) => s + r.open_rate, 0) / data.length
        : 0,
    [data],
  );

  if (data === null) return <Loading />;
  if (data.length === 0) return <Empty msg="No domain data yet." />;

  const lowOpen = (r: DomainRow) =>
    avgOpen > 0 && r.open_rate < avgOpen * 0.5
      ? "text-rose-400"
      : "";

  return (
    <div>
      <Table>
        <THead>
          <TR>
            <TH>Domain</TH>
            <TH className="text-right">Recipients</TH>
            <TH className="text-right">Delivered</TH>
            <TH className="text-right">Open</TH>
            <TH className="text-right">Click</TH>
            <TH className="text-right">Bounce</TH>
            <TH className="text-right">Complaint</TH>
          </TR>
        </THead>
        <TBody>
          {data.map((r) => (
            <TR key={r.domain || "(empty)"}>
              <TD className="font-mono text-xs">{r.domain || "(empty)"}</TD>
              <TD className="text-right">{r.recipients.toLocaleString()}</TD>
              <TD className="text-right">{r.delivered.toLocaleString()}</TD>
              <TD className={`text-right ${lowOpen(r)}`}>{pct(r.open_rate)}</TD>
              <TD className="text-right">{pct(r.click_rate)}</TD>
              <TD className="text-right">{pct(r.bounce_rate, 2)}</TD>
              <TD className="text-right">{pct(r.complaint_rate, 3)}</TD>
            </TR>
          ))}
        </TBody>
      </Table>
      <p className="text-xs text-zinc-600 mt-3">
        Red open rate = more than 50% below the average across visible domains. Strong signal that mail to that ISP is landing in spam.
      </p>
    </div>
  );
}

// ─── Top clicked links ────────────────────────────────────────────────────

function LinksTab({ windowDays }: { windowDays: Window }) {
  const [data, setData] = useState<LinkRow[] | null>(null);
  useEffect(() => {
    setData(null);
    fetch(`/api/email/top-links?days=${windowDays}`)
      .then((r) => r.json())
      .then(setData)
      .catch(() => setData([]));
  }, [windowDays]);
  if (data === null) return <Loading />;
  if (data.length === 0) return <Empty msg="No clicks recorded yet." />;
  return (
    <Table>
      <THead>
        <TR>
          <TH>URL</TH>
          <TH className="text-right">Clicks</TH>
          <TH className="text-right">Unique clickers</TH>
          <TH className="text-right">Broadcasts</TH>
        </TR>
      </THead>
      <TBody>
        {data.map((r) => (
          <TR key={r.url}>
            <TD className="font-mono text-xs max-w-[480px] truncate" title={r.url}>{r.url}</TD>
            <TD className="text-right">{r.clicks.toLocaleString()}</TD>
            <TD className="text-right">{r.unique_clickers.toLocaleString()}</TD>
            <TD className="text-right text-zinc-500">{r.broadcasts.length}</TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

// ─── Send time (hour + day of week) ───────────────────────────────────────

const DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function SendTimesTab({ windowDays }: { windowDays: Window }) {
  const [data, setData] = useState<SendTimeReport | null>(null);
  useEffect(() => {
    setData(null);
    fetch(`/api/email/send-times?days=${windowDays}`)
      .then((r) => r.json())
      .then(setData)
      .catch(() => setData(null));
  }, [windowDays]);
  if (data === null) return <Loading />;
  const totalDelivered = data.hours.reduce((s, h) => s + h.delivered, 0);
  if (totalDelivered === 0)
    return <Empty msg="Not enough delivered mail to compute timing yet." />;

  const hourData = data.hours.map((h) => ({
    label: `${String(h.hour).padStart(2, "0")}:00`,
    open_rate_pct: +(h.open_rate * 100).toFixed(2),
    delivered: h.delivered,
  }));
  const dowData = data.dow.map((d) => ({
    label: DOW_LABELS[d.dow],
    open_rate_pct: +(d.open_rate * 100).toFixed(2),
    delivered: d.delivered,
  }));

  return (
    <div className="space-y-6">
      <div>
        <div className="text-sm text-zinc-300 mb-2">Open rate by hour of day (UTC)</div>
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={hourData}>
            <CartesianGrid stroke="#27272a" strokeDasharray="3 3" />
            <XAxis dataKey="label" stroke="#71717a" fontSize={10} />
            <YAxis stroke="#71717a" fontSize={11} unit="%" />
            <Tooltip
              contentStyle={{
                background: "#18181b",
                border: "1px solid #27272a",
                borderRadius: 8,
              }}
            />
            <Bar dataKey="open_rate_pct" fill="#3b82f6" name="open rate" />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div>
        <div className="text-sm text-zinc-300 mb-2">Open rate by day of week (UTC)</div>
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={dowData}>
            <CartesianGrid stroke="#27272a" strokeDasharray="3 3" />
            <XAxis dataKey="label" stroke="#71717a" fontSize={11} />
            <YAxis stroke="#71717a" fontSize={11} unit="%" />
            <Tooltip
              contentStyle={{
                background: "#18181b",
                border: "1px solid #27272a",
                borderRadius: 8,
              }}
            />
            <Bar dataKey="open_rate_pct" fill="#8b5cf6" name="open rate" />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

// ─── Engagement segmentation ──────────────────────────────────────────────

const SEGMENT_COLORS: Record<string, string> = {
  active: "bg-emerald-500",
  lapsed: "bg-amber-500",
  inactive: "bg-orange-500",
  never_opened: "bg-rose-500",
};

const SEGMENT_LABELS: Record<string, string> = {
  active: "Active (last 30d)",
  lapsed: "Lapsed (30-90d)",
  inactive: "Inactive (>90d)",
  never_opened: "Never opened",
};

function EngagementTab() {
  const [data, setData] = useState<EngagementSegment[] | null>(null);
  useEffect(() => {
    fetch(`/api/email/engagement`)
      .then((r) => r.json())
      .then(setData)
      .catch(() => setData([]));
  }, []);
  if (data === null) return <Loading />;
  if (data.length === 0 || data.every((s) => s.recipients === 0))
    return <Empty msg="No delivered recipients yet." />;
  const total = data.reduce((s, r) => s + r.recipients, 0);

  return (
    <div className="space-y-4">
      <div className="flex h-6 rounded overflow-hidden border border-zinc-800">
        {data.map((s) => (
          <div
            key={s.segment}
            className={SEGMENT_COLORS[s.segment] || "bg-zinc-700"}
            style={{ width: `${(s.pct_of_list * 100).toFixed(2)}%` }}
            title={`${SEGMENT_LABELS[s.segment]}: ${s.recipients}`}
          />
        ))}
      </div>
      <Table>
        <THead>
          <TR>
            <TH>Segment</TH>
            <TH className="text-right">Recipients</TH>
            <TH className="text-right">% of list</TH>
          </TR>
        </THead>
        <TBody>
          {data.map((s) => (
            <TR key={s.segment}>
              <TD>{SEGMENT_LABELS[s.segment]}</TD>
              <TD className="text-right">{s.recipients.toLocaleString()}</TD>
              <TD className="text-right">{pct(s.pct_of_list)}</TD>
            </TR>
          ))}
        </TBody>
      </Table>
      <div className="text-xs text-zinc-500">
        Total addressable recipients: {total.toLocaleString()}
      </div>
    </div>
  );
}

// ─── Broadcasts (V2 with rates + complaint sort) ──────────────────────────

function BroadcastsTab() {
  const [data, setData] = useState<BroadcastRowV2[] | null>(null);
  useEffect(() => {
    fetch("/api/email/broadcasts")
      .then((r) => r.json())
      .then((rows: BroadcastRowV2[]) => {
        // Sort by complaint rate desc by default — surfaces problem broadcasts.
        const sorted = [...rows].sort(
          (a, b) => b.complaint_rate - a.complaint_rate,
        );
        setData(sorted);
      })
      .catch(() => setData([]));
  }, []);
  if (data === null) return <Loading />;
  if (data.length === 0) return <Empty msg="No broadcasts tagged yet." />;
  return (
    <Table>
      <THead>
        <TR>
          <TH>Broadcast</TH>
          <TH className="text-right">Sent</TH>
          <TH className="text-right">Deliv.</TH>
          <TH className="text-right">Open</TH>
          <TH className="text-right">Click</TH>
          <TH className="text-right">CTOR</TH>
          <TH className="text-right">Bounce</TH>
          <TH className="text-right">Complaint</TH>
        </TR>
      </THead>
      <TBody>
        {data.map((r) => (
          <TR key={r.broadcast_id}>
            <TD className="font-mono text-xs">{r.broadcast_id}</TD>
            <TD className="text-right">{r.sent.toLocaleString()}</TD>
            <TD className="text-right">{pct(r.delivery_rate)}</TD>
            <TD className="text-right">{pct(r.open_rate)}</TD>
            <TD className="text-right">{pct(r.click_rate)}</TD>
            <TD className="text-right">{pct(r.ctor)}</TD>
            <TD className="text-right">{pct(r.bounce_rate, 2)}</TD>
            <TD
              className={`text-right ${
                r.complaint_rate > 0.001 ? "text-rose-400" : ""
              }`}
            >
              {pct(r.complaint_rate, 3)}
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

// ─── Bounces (sub-tabs: hard / soft / suppressions) ───────────────────────

function BouncesTab({ windowDays }: { windowDays: Window }) {
  const [sub, setSub] = useState<"hard" | "soft" | "suppressions">("hard");
  return (
    <div>
      <div className="mb-4">
        <Tabs
          value={sub}
          onChange={(v) => setSub(v as typeof sub)}
          options={[
            { value: "hard", label: "Hard bounces" },
            { value: "soft", label: "Soft bounces" },
            { value: "suppressions", label: "Suppression log" },
          ]}
        />
      </div>
      {sub === "hard" && <HardBouncesTab />}
      {sub === "soft" && <SoftBouncesTab windowDays={windowDays} />}
      {sub === "suppressions" && <SuppressionsTab />}
    </div>
  );
}

function HardBouncesTab() {
  const [data, setData] = useState<BounceRow[] | null>(null);
  useEffect(() => {
    fetch("/api/email/bounces")
      .then((r) => r.json())
      .then(setData)
      .catch(() => setData([]));
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

function SoftBouncesTab({ windowDays }: { windowDays: Window }) {
  const [data, setData] = useState<SoftBounceRow[] | null>(null);
  useEffect(() => {
    setData(null);
    fetch(`/api/email/soft-bounces?days=${windowDays}`)
      .then((r) => r.json())
      .then(setData)
      .catch(() => setData([]));
  }, [windowDays]);
  if (data === null) return <Loading />;
  if (data.length === 0) return <Empty msg="No soft bounces in window." />;
  return (
    <Table>
      <THead>
        <TR>
          <TH>Recipient</TH>
          <TH className="text-right">Count</TH>
          <TH>Last bounce</TH>
          <TH>Reason</TH>
        </TR>
      </THead>
      <TBody>
        {data.map((r) => (
          <TR key={r.recipient}>
            <TD className="font-mono text-xs">{r.recipient}</TD>
            <TD className={`text-right ${r.count >= 2 ? "text-amber-400" : ""}`}>
              {r.count}
            </TD>
            <TD>{formatRelativeTime(r.last_bounced_at)}</TD>
            <TD className="text-zinc-400 text-xs">{r.last_message ?? "—"}</TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function SuppressionsTab() {
  const [data, setData] = useState<SuppressionLogRow[] | null>(null);
  useEffect(() => {
    fetch("/api/email/suppressions")
      .then((r) => r.json())
      .then(setData)
      .catch(() => setData([]));
  }, []);
  if (data === null) return <Loading />;
  if (data.length === 0) return <Empty msg="Nothing suppressed yet." />;
  return (
    <Table>
      <THead>
        <TR>
          <TH>Recipient</TH>
          <TH>When</TH>
          <TH>Reason</TH>
        </TR>
      </THead>
      <TBody>
        {data.map((r) => (
          <TR key={`${r.email}-${r.suppressed_at}`}>
            <TD className="font-mono text-xs">{r.email}</TD>
            <TD>{formatRelativeTime(r.suppressed_at)}</TD>
            <TD className="text-zinc-400 text-xs">{r.suppressed_reason || "—"}</TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

// ─── Unsubscribes ─────────────────────────────────────────────────────────

function UnsubsTab({ windowDays }: { windowDays: Window }) {
  const [data, setData] = useState<UnsubscribeReport | null>(null);
  useEffect(() => {
    setData(null);
    fetch(`/api/email/unsubscribes?days=${windowDays}`)
      .then((r) => r.json())
      .then(setData)
      .catch(() => setData(null));
  }, [windowDays]);
  if (data === null) return <Loading />;
  if (data.recent.length === 0 && data.trend.length === 0)
    return <Empty msg="No unsubscribes recorded yet." />;

  return (
    <div className="space-y-6">
      {data.trend.length > 0 ? (
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={data.trend}>
            <CartesianGrid stroke="#27272a" strokeDasharray="3 3" />
            <XAxis dataKey="day" stroke="#71717a" fontSize={11} />
            <YAxis stroke="#71717a" fontSize={11} />
            <Tooltip
              contentStyle={{
                background: "#18181b",
                border: "1px solid #27272a",
                borderRadius: 8,
              }}
            />
            <Bar dataKey="count" fill="#ef4444" name="unsubscribes" />
          </BarChart>
        </ResponsiveContainer>
      ) : null}
      <Table>
        <THead>
          <TR>
            <TH>Recipient</TH>
            <TH>Source</TH>
            <TH>When</TH>
          </TR>
        </THead>
        <TBody>
          {data.recent.map((r) => (
            <TR key={`${r.recipient}-${r.unsubscribed_at}`}>
              <TD className="font-mono text-xs">{r.recipient}</TD>
              <TD className="text-zinc-400 text-xs">{r.source || "—"}</TD>
              <TD>{formatRelativeTime(r.unsubscribed_at)}</TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </div>
  );
}

// ─── Queue (unchanged) ────────────────────────────────────────────────────

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

// ─── Sequence heatmap (unchanged) ─────────────────────────────────────────

function SequenceTab() {
  const [data, setData] = useState<SequenceCell[] | null>(null);
  useEffect(() => {
    fetch("/api/email/sequence").then((r) => r.json()).then(setData).catch(() => setData([]));
  }, []);
  if (data === null) return <Loading />;
  if (data.length === 0) return <Empty msg="No sequence sends recorded yet." />;
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

// ─── Exit reasons (unchanged) ─────────────────────────────────────────────

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
        const pctVal = (r.count / total) * 100;
        return (
          <div key={r.exit_reason}>
            <div className="flex justify-between text-sm mb-1">
              <span>{r.exit_reason || "(no reason)"}</span>
              <span className="text-zinc-400">
                {r.count.toLocaleString()} ({pctVal.toFixed(1)}%)
              </span>
            </div>
            <div className="h-2 bg-zinc-800 rounded overflow-hidden">
              <div
                className="h-full bg-blue-500"
                style={{ width: `${pctVal}%` }}
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
