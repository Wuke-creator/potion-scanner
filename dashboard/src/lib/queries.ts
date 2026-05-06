// All SQL queries for the dashboard live here. Each function opens the
// minimum set of DBs it needs via lib/db. Returns plain JS row shapes
// matching lib/types.

import { openDb, tryOpenDb } from "./db";
import type {
  SummaryCounts,
  TicketRow,
  TicketStatus,
  TicketSource,
  ChannelStats,
  CallerStats,
  SignalFeedRow,
  PnLBucket,
  FunnelStage,
  QueueRow,
  EmailKpis,
  BroadcastRow,
  BounceRow,
  SequenceCell,
  ExitReasonRow,
  LeadershipMention,
  StaffMember,
} from "./types";
import { seniorStaff, staffName } from "./staff";
import { classifyComplaint, isComplaint } from "./complaints";

const DAY = 86400;

function nowSec(): number {
  return Math.floor(Date.now() / 1000);
}

// ─── summary (home view) ─────────────────────────────────────────────

export function getSummary(): SummaryCounts {
  const ops = tryOpenDb("ops");
  const analytics = openDb("analytics");
  const verified = openDb("verified");
  const email = openDb("email");
  const emailEvents = tryOpenDb("email_events");
  const whop = tryOpenDb("whop_members");

  const now = nowSec();
  const day = now - DAY;
  const twoDay = now - 2 * DAY;

  const openTickets = ops
    ? (ops.prepare("SELECT COUNT(*) AS c FROM tickets WHERE status = 'open' AND source = 'ticket'").get() as { c: number }).c
    : 0;

  const ticketsTrend14d: number[] = [];
  if (ops) {
    const rows = ops
      .prepare(
        "SELECT (created_at / 86400) AS day_index, COUNT(*) AS c FROM tickets " +
          "WHERE source = 'ticket' AND created_at >= ? GROUP BY day_index ORDER BY day_index"
      )
      .all(now - 14 * DAY) as { day_index: number; c: number }[];
    const startDay = Math.floor((now - 14 * DAY) / DAY);
    for (let i = 0; i < 14; i++) {
      const found = rows.find((r) => r.day_index === startDay + i);
      ticketsTrend14d.push(found ? found.c : 0);
    }
  } else {
    for (let i = 0; i < 14; i++) ticketsTrend14d.push(0);
  }

  // Open customer complaints across all 3 monitored channels. Pulled via
  // listTickets in complaints mode so the keyword classifier runs on the
  // captured rows. Limited to a wide page so the count is accurate even
  // when general/alpha is noisy.
  const openComplaintRows = listTickets({ status: "open", mode: "complaints", limit: 5000 });
  const openComplaints = openComplaintRows.length;
  const openComplaintsHigh = openComplaintRows.filter((r) => r.complaint_severity === "high").length;

  const complaintsTrend14d: number[] = new Array(14).fill(0);
  if (ops) {
    const recent = listTickets({
      status: "all",
      mode: "complaints",
      limit: 10000,
    }).filter((r) => r.created_at >= now - 14 * DAY);
    const startDay = Math.floor((now - 14 * DAY) / DAY);
    for (const r of recent) {
      const idx = Math.floor(r.created_at / DAY) - startDay;
      if (idx >= 0 && idx < 14) complaintsTrend14d[idx] = (complaintsTrend14d[idx] ?? 0) + 1;
    }
  }

  const signalsToday = (analytics
    .prepare("SELECT COUNT(*) AS c FROM trades WHERE opened_at >= ?")
    .get(day) as { c: number }).c;
  const signalsYesterday = (analytics
    .prepare("SELECT COUNT(*) AS c FROM trades WHERE opened_at >= ? AND opened_at < ?")
    .get(twoDay, day) as { c: number }).c;

  const verifiedActive = (verified
    .prepare("SELECT COUNT(*) AS c FROM verified_users WHERE is_active = 1")
    .get() as { c: number }).c;

  const emailQueueDepth = (email
    .prepare("SELECT COUNT(*) AS c FROM scheduled_sends WHERE status = 'pending'")
    .get() as { c: number }).c;

  let bounceRate24h = 0;
  let bounceTotal24h = 0;
  if (emailEvents) {
    const total = (emailEvents
      .prepare(
        "SELECT COUNT(*) AS c FROM email_events WHERE event_at >= ? AND event_type IN ('delivered','bounced')"
      )
      .get(day) as { c: number }).c;
    const bounced = (emailEvents
      .prepare("SELECT COUNT(*) AS c FROM email_events WHERE event_at >= ? AND event_type = 'bounced'")
      .get(day) as { c: number }).c;
    bounceRate24h = total ? bounced / total : 0;
    bounceTotal24h = total;
  }

  // The dunning_active column is added by a 2026-04-18 migration. Older
  // whop_members.db files don't have it; treat as 0 instead of crashing.
  let dunningActive = 0;
  if (whop) {
    try {
      dunningActive = (whop
        .prepare("SELECT COUNT(*) AS c FROM whop_members WHERE dunning_active = 1")
        .get() as { c: number }).c;
    } catch {
      dunningActive = 0;
    }
  }

  const unackedLeadership = ops
    ? (ops
        .prepare("SELECT COUNT(*) AS c FROM leadership_mentions WHERE acknowledged = 0")
        .get() as { c: number }).c
    : 0;

  // Missed calls: stop_hit + canceled trade events in last 7d, plus open
  // "Caller-ticket-*" threads in the support forum if ops capture is on.
  const sevenDay = now - 7 * DAY;
  const missedCallEvents = (analytics
    .prepare(
      "SELECT COUNT(*) c FROM trade_events WHERE event_type IN ('stop_hit','canceled') AND recorded_at >= ?"
    )
    .get(sevenDay) as { c: number }).c;
  const stopHits = (analytics
    .prepare("SELECT COUNT(*) c FROM trade_events WHERE event_type='stop_hit' AND recorded_at >= ?")
    .get(sevenDay) as { c: number }).c;
  const canceledCalls = (analytics
    .prepare("SELECT COUNT(*) c FROM trade_events WHERE event_type='canceled' AND recorded_at >= ?")
    .get(sevenDay) as { c: number }).c;
  const openCallerTickets = ops
    ? (ops
        .prepare(
          "SELECT COUNT(*) c FROM tickets WHERE status='open' AND source='ticket' AND thread_name LIKE 'Caller-ticket%'"
        )
        .get() as { c: number }).c
    : 0;
  const missedCalls7d = missedCallEvents + openCallerTickets;

  // 14d sparkline of stop_hit + canceled events.
  const missedCallsTrend14d: number[] = [];
  const trendRows = analytics
    .prepare(
      "SELECT (recorded_at / 86400) AS day_index, COUNT(*) c FROM trade_events " +
        "WHERE event_type IN ('stop_hit','canceled') AND recorded_at >= ? GROUP BY day_index ORDER BY day_index"
    )
    .all(now - 14 * DAY) as { day_index: number; c: number }[];
  const startDay = Math.floor((now - 14 * DAY) / DAY);
  for (let i = 0; i < 14; i++) {
    const found = trendRows.find((r) => r.day_index === startDay + i);
    missedCallsTrend14d.push(found ? found.c : 0);
  }

  // Email pipeline last-activity probe: prefer email_events (real Resend
  // webhooks), fall back to scheduled_sends.sent_at.
  let lastEmailActivityAt: number | null = null;
  if (emailEvents) {
    const r = emailEvents.prepare("SELECT MAX(event_at) AS m FROM email_events").get() as { m: number | null };
    lastEmailActivityAt = r.m ?? null;
  }
  if (!lastEmailActivityAt) {
    const r = email
      .prepare("SELECT MAX(sent_at) AS m FROM scheduled_sends WHERE sent_at IS NOT NULL")
      .get() as { m: number | null };
    lastEmailActivityAt = r.m ?? null;
  }

  return {
    openTickets,
    ticketsTrend14d,
    openComplaints,
    openComplaintsHigh,
    complaintsTrend14d,
    signalsToday,
    signalsYesterday,
    verifiedActive,
    emailQueueDepth,
    bounceRate24h,
    bounceTotal24h,
    dunningActive,
    unackedLeadership,
    missedCalls7d,
    missedCallsTrend14d,
    missedCallsBreakdown: {
      stop_hit: stopHits,
      canceled: canceledCalls,
      open_caller_tickets: openCallerTickets,
    },
    lastEmailActivityAt,
  };
}

// ─── tickets ─────────────────────────────────────────────────────────

export function listTickets(opts: {
  status?: TicketStatus | "all";
  source?: "ticket" | "general" | "alpha" | "all";
  // "complaints" = need-support threads + general/alpha messages flagged
  // by the keyword classifier. "all" = every captured message.
  mode?: "complaints" | "all";
  // Optional severity floor: 'low' includes low+medium+high, etc. Only
  // applies in complaints mode.
  minSeverity?: "low" | "medium" | "high";
  limit?: number;
}): TicketRow[] {
  const ops = tryOpenDb("ops");
  if (!ops) return [];
  const conditions: string[] = [];
  const params: (string | number)[] = [];
  if (opts.status && opts.status !== "all") {
    conditions.push("status = ?");
    params.push(opts.status);
  }
  if (opts.source && opts.source !== "all") {
    conditions.push("source = ?");
    params.push(opts.source);
  }
  const where = conditions.length ? `WHERE ${conditions.join(" AND ")}` : "";
  // Pull a wider page than the final cap so filtering doesn't starve
  // the result set on noisy chat channels.
  const limit = opts.limit ?? 200;
  const fetchCap = opts.mode === "complaints" ? Math.max(limit * 5, 1000) : limit;
  // SQLite returns author_is_staff as 0/1 ints. The TicketRow public
  // type has it as boolean. Use a private "raw" shape here so the
  // spread that maps raw -> TicketRow doesn't hit a never-type
  // conflict (boolean & number = never).
  type RawTicketRow = Omit<TicketRow, "author_is_staff" | "complaint_severity"> & {
    author_is_staff: number;
  };
  const rows = ops
    .prepare(
      `SELECT message_id, channel_id, channel_name, parent_id, thread_name, source,
              author_id, author_name, author_is_staff, content, created_at,
              captured_at, status, staff_notes, last_action_at, jump_url
         FROM tickets
         ${where}
         ORDER BY created_at DESC
         LIMIT ?`
    )
    .all(...params, fetchCap) as RawTicketRow[];

  const severityRank: Record<string, number> = { none: 0, low: 1, medium: 2, high: 3 };
  const minRank = opts.minSeverity ? severityRank[opts.minSeverity] : 0;

  const enriched: TicketRow[] = rows.map((r) => ({
    ...r,
    author_is_staff: !!r.author_is_staff,
    complaint_severity: classifyComplaint(r.content),
  }));

  let filtered = enriched;
  if (opts.mode === "complaints") {
    filtered = enriched.filter((r) => {
      if (r.source === "ticket") return true;
      if (severityRank[r.complaint_severity] < Math.max(minRank, 1)) return false;
      return isComplaint(r.source as TicketSource, r.content);
    });
  } else if (opts.minSeverity) {
    filtered = enriched.filter((r) => severityRank[r.complaint_severity] >= minRank);
  }

  return filtered.slice(0, limit);
}

export function getTicket(messageId: number): TicketRow | null {
  const ops = tryOpenDb("ops");
  if (!ops) return null;
  // Same RawTicketRow trick as listTickets above — boolean & number
  // intersects to never, so use Omit + override to keep the spread valid.
  type RawTicketRow = Omit<TicketRow, "author_is_staff" | "complaint_severity"> & {
    author_is_staff: number;
  };
  const r = ops
    .prepare(
      `SELECT message_id, channel_id, channel_name, parent_id, thread_name, source,
              author_id, author_name, author_is_staff, content, created_at,
              captured_at, status, staff_notes, last_action_at, jump_url
         FROM tickets WHERE message_id = ?`
    )
    .get(messageId) as RawTicketRow | undefined;
  if (!r) return null;
  return {
    ...r,
    author_is_staff: !!r.author_is_staff,
    complaint_severity: classifyComplaint(r.content),
  };
}

export function updateTicketStatus(
  messageId: number,
  status: TicketStatus,
  notes?: string
): TicketRow | null {
  const ops = openDb("ops", { readonly: false });
  const now = nowSec();
  if (typeof notes === "string") {
    ops
      .prepare("UPDATE tickets SET status = ?, staff_notes = ?, last_action_at = ? WHERE message_id = ?")
      .run(status, notes, now, messageId);
  } else {
    ops
      .prepare("UPDATE tickets SET status = ?, last_action_at = ? WHERE message_id = ?")
      .run(status, now, messageId);
  }
  return getTicket(messageId);
}

// ─── signals (analytics.db) ──────────────────────────────────────────

interface RawTradeWithLastEvent {
  trade_id: number;
  channel_key: string;
  pair: string;
  side: string;
  entry: number;
  leverage: number;
  opened_at: number;
  source_discord_user_id: string | null;
  last_event_type: string | null;
  last_event_pnl: number | null;
  last_event_at: number | null;
}

const TERMINAL_EVENTS = new Set(["all_tp_hit", "stop_hit", "trade_closed", "canceled"]);
const WIN_EVENTS = new Set(["tp_hit", "all_tp_hit"]);

function channelStatsFromRows(rows: RawTradeWithLastEvent[]): ChannelStats[] {
  const byChannel: Record<string, RawTradeWithLastEvent[]> = {};
  for (const r of rows) (byChannel[r.channel_key] ||= []).push(r);
  return Object.entries(byChannel).map(([channelKey, ts]) => {
    const closed = ts.filter((t) => t.last_event_type && TERMINAL_EVENTS.has(t.last_event_type));
    const wins = closed.filter((t) => t.last_event_type && WIN_EVENTS.has(t.last_event_type));
    const pnls = closed
      .map((t) => t.last_event_pnl)
      .filter((p): p is number => typeof p === "number");
    const avg = pnls.length ? pnls.reduce((a, b) => a + b, 0) / pnls.length : 0;
    const best = pnls.length ? Math.max(...pnls) : null;
    const worst = pnls.length ? Math.min(...pnls) : null;
    const bestRow = best !== null ? closed.find((t) => t.last_event_pnl === best) : null;
    const worstRow = worst !== null ? closed.find((t) => t.last_event_pnl === worst) : null;
    return {
      channel_key: channelKey,
      signal_count_7d: ts.filter((t) => t.opened_at >= nowSec() - 7 * DAY).length,
      signal_count_30d: ts.length,
      win_count: wins.length,
      closed_count: closed.length,
      win_rate: closed.length ? wins.length / closed.length : 0,
      avg_pnl_pct: avg,
      best_pnl_pct: best,
      best_trade_pair: bestRow?.pair ?? null,
      best_trade_id: bestRow?.trade_id ?? null,
      worst_pnl_pct: worst,
      worst_trade_pair: worstRow?.pair ?? null,
      worst_trade_id: worstRow?.trade_id ?? null,
    };
  });
}

// Column-existence cache. The bot adds source_discord_user_id via a
// 2026-05 migration; older local copies of analytics.db don't have it.
// We detect once at first use and conditionally select the column so the
// dashboard works on both pre- and post-migration databases.
let _hasSourceUserCol: boolean | null = null;

function hasSourceUserColumn(): boolean {
  if (_hasSourceUserCol !== null) return _hasSourceUserCol;
  const analytics = openDb("analytics");
  const cols = analytics.prepare("PRAGMA table_info(trades)").all() as { name: string }[];
  _hasSourceUserCol = cols.some((c) => c.name === "source_discord_user_id");
  return _hasSourceUserCol;
}

function fetchTradesWithLastEvent(sinceEpoch: number): RawTradeWithLastEvent[] {
  const analytics = openDb("analytics");
  const userCol = hasSourceUserColumn() ? "t.source_discord_user_id" : "NULL AS source_discord_user_id";
  // For each trade, find the most recent lifecycle event by recorded_at.
  // Single-pass: outer trades, lateral subquery for last event.
  return analytics
    .prepare(
      `SELECT t.trade_id, t.channel_key, t.pair, t.side, t.entry, t.leverage, t.opened_at,
              ${userCol},
              (SELECT e.event_type FROM trade_events e
                WHERE e.trade_id = t.trade_id AND e.channel_key = t.channel_key
                ORDER BY e.recorded_at DESC LIMIT 1) AS last_event_type,
              (SELECT e.pnl_pct FROM trade_events e
                WHERE e.trade_id = t.trade_id AND e.channel_key = t.channel_key
                ORDER BY e.recorded_at DESC LIMIT 1) AS last_event_pnl,
              (SELECT e.recorded_at FROM trade_events e
                WHERE e.trade_id = t.trade_id AND e.channel_key = t.channel_key
                ORDER BY e.recorded_at DESC LIMIT 1) AS last_event_at
         FROM trades t
         WHERE t.opened_at >= ?
         ORDER BY t.opened_at DESC`
    )
    .all(sinceEpoch) as RawTradeWithLastEvent[];
}

export function getChannelStats(): ChannelStats[] {
  const rows = fetchTradesWithLastEvent(nowSec() - 30 * DAY);
  return channelStatsFromRows(rows);
}

export function getCallerStats(): CallerStats[] {
  const rows = fetchTradesWithLastEvent(nowSec() - 30 * DAY).filter(
    (r) => r.source_discord_user_id
  );
  const byCaller: Record<string, RawTradeWithLastEvent[]> = {};
  for (const r of rows) {
    const id = r.source_discord_user_id as string;
    (byCaller[id] ||= []).push(r);
  }
  return Object.entries(byCaller).map(([callerId, ts]) => {
    const closed = ts.filter((t) => t.last_event_type && TERMINAL_EVENTS.has(t.last_event_type));
    const wins = closed.filter((t) => t.last_event_type && WIN_EVENTS.has(t.last_event_type));
    const pnls = closed.map((t) => t.last_event_pnl).filter((p): p is number => typeof p === "number");
    const avg = pnls.length ? pnls.reduce((a, b) => a + b, 0) / pnls.length : 0;
    return {
      source_discord_user_id: callerId,
      signal_count_7d: ts.filter((t) => t.opened_at >= nowSec() - 7 * DAY).length,
      signal_count_30d: ts.length,
      win_count: wins.length,
      closed_count: closed.length,
      win_rate: closed.length ? wins.length / closed.length : 0,
      avg_pnl_pct: avg,
    };
  });
}

export function legacyUntrackedSignalCount(): number {
  const analytics = openDb("analytics");
  if (!hasSourceUserColumn()) {
    // Pre-migration: every row is "legacy" because the column doesn't exist.
    return (analytics.prepare("SELECT COUNT(*) AS c FROM trades").get() as { c: number }).c;
  }
  return (analytics
    .prepare("SELECT COUNT(*) AS c FROM trades WHERE source_discord_user_id IS NULL")
    .get() as { c: number }).c;
}

export function getRecentSignals(limit = 50): SignalFeedRow[] {
  return fetchTradesWithLastEvent(nowSec() - 60 * DAY).slice(0, limit);
}

export function getPnLDistribution(): PnLBucket[] {
  const analytics = openDb("analytics");
  const rows = analytics
    .prepare(
      "SELECT pnl_pct FROM trade_events WHERE pnl_pct IS NOT NULL AND event_type IN ('all_tp_hit','tp_hit','stop_hit','trade_closed')"
    )
    .all() as { pnl_pct: number }[];
  const buckets: PnLBucket[] = [
    { range: "<-50%", bucket_min: -Infinity, bucket_max: -50, count: 0 },
    { range: "-50..-25%", bucket_min: -50, bucket_max: -25, count: 0 },
    { range: "-25..0%", bucket_min: -25, bucket_max: 0, count: 0 },
    { range: "0..25%", bucket_min: 0, bucket_max: 25, count: 0 },
    { range: "25..50%", bucket_min: 25, bucket_max: 50, count: 0 },
    { range: "50..100%", bucket_min: 50, bucket_max: 100, count: 0 },
    { range: "100..200%", bucket_min: 100, bucket_max: 200, count: 0 },
    { range: "200%+", bucket_min: 200, bucket_max: Infinity, count: 0 },
  ];
  for (const { pnl_pct } of rows) {
    for (const b of buckets) {
      if (pnl_pct >= b.bucket_min && pnl_pct < b.bucket_max) {
        b.count += 1;
        break;
      }
    }
  }
  return buckets;
}

export function getFunnelStages(): FunnelStage[] {
  const analytics = openDb("analytics");
  const totalSignals = (analytics
    .prepare("SELECT COUNT(*) AS c FROM trades WHERE opened_at >= ?")
    .get(nowSec() - 30 * DAY) as { c: number }).c;
  const eventCount = (eventType: string) =>
    (analytics
      .prepare(
        "SELECT COUNT(DISTINCT trade_id || '/' || channel_key) AS c FROM trade_events WHERE event_type = ? AND recorded_at >= ?"
      )
      .get(eventType, nowSec() - 30 * DAY) as { c: number }).c;
  const breakeven = eventCount("breakeven");
  const tp1 = (analytics
    .prepare(
      "SELECT COUNT(DISTINCT trade_id || '/' || channel_key) AS c FROM trade_events WHERE event_type = 'tp_hit' AND tp_number >= 1 AND recorded_at >= ?"
    )
    .get(nowSec() - 30 * DAY) as { c: number }).c;
  const tp2 = (analytics
    .prepare(
      "SELECT COUNT(DISTINCT trade_id || '/' || channel_key) AS c FROM trade_events WHERE event_type = 'tp_hit' AND tp_number >= 2 AND recorded_at >= ?"
    )
    .get(nowSec() - 30 * DAY) as { c: number }).c;
  const tp3 = (analytics
    .prepare(
      "SELECT COUNT(DISTINCT trade_id || '/' || channel_key) AS c FROM trade_events WHERE event_type = 'tp_hit' AND tp_number >= 3 AND recorded_at >= ?"
    )
    .get(nowSec() - 30 * DAY) as { c: number }).c;
  const allTp = eventCount("all_tp_hit");
  const stop = eventCount("stop_hit");
  const cancelled = eventCount("canceled");
  return [
    { stage: "Signal", count: totalSignals },
    { stage: "Breakeven", count: breakeven },
    { stage: "TP1", count: tp1 },
    { stage: "TP2", count: tp2 },
    { stage: "TP3", count: tp3 },
    { stage: "All TP", count: allTp },
    { stage: "Stop hit", count: stop },
    { stage: "Cancelled", count: cancelled },
  ];
}

// ─── email ───────────────────────────────────────────────────────────

export function getEmailQueue(): QueueRow[] {
  const email = openDb("email");
  return email
    .prepare(
      "SELECT sequence, COUNT(*) AS pending FROM scheduled_sends WHERE status = 'pending' GROUP BY sequence ORDER BY pending DESC"
    )
    .all() as QueueRow[];
}

export function getEmailKpis(): EmailKpis {
  const ev = tryOpenDb("email_events");
  if (!ev) {
    return {
      sent: 0, delivered: 0, opened: 0, clicked: 0, bounced: 0, complained: 0,
      open_rate: 0, click_rate: 0, bounce_rate: 0,
    };
  }
  const since = nowSec() - 7 * DAY;
  const counts = ev
    .prepare(
      "SELECT event_type, COUNT(*) AS c FROM email_events WHERE event_at >= ? GROUP BY event_type"
    )
    .all(since) as { event_type: string; c: number }[];
  const map: Record<string, number> = {};
  for (const r of counts) map[r.event_type] = r.c;
  const sent = map.sent ?? 0;
  const delivered = map.delivered ?? 0;
  const opened = map.opened ?? 0;
  const clicked = map.clicked ?? 0;
  const bounced = map.bounced ?? 0;
  const complained = map.complained ?? 0;
  return {
    sent,
    delivered,
    opened,
    clicked,
    bounced,
    complained,
    open_rate: delivered ? opened / delivered : 0,
    click_rate: delivered ? clicked / delivered : 0,
    bounce_rate: sent ? bounced / sent : 0,
  };
}

export function getBroadcasts(): BroadcastRow[] {
  const ev = tryOpenDb("email_events");
  if (!ev) return [];
  const rows = ev
    .prepare(
      `SELECT broadcast_id,
              SUM(CASE WHEN event_type='delivered' THEN 1 ELSE 0 END) AS delivered,
              SUM(CASE WHEN event_type='opened' THEN 1 ELSE 0 END) AS opened,
              SUM(CASE WHEN event_type='clicked' THEN 1 ELSE 0 END) AS clicked,
              SUM(CASE WHEN event_type='bounced' THEN 1 ELSE 0 END) AS bounced,
              SUM(CASE WHEN event_type='complained' THEN 1 ELSE 0 END) AS complained,
              COUNT(*) AS total
         FROM email_events
        WHERE broadcast_id IS NOT NULL AND broadcast_id != ''
        GROUP BY broadcast_id
        ORDER BY MAX(event_at) DESC
        LIMIT 30`
    )
    .all() as BroadcastRow[];
  return rows;
}

export function getHardBounces(limit = 100): BounceRow[] {
  const ev = tryOpenDb("email_events");
  if (!ev) return [];
  return ev
    .prepare(
      `SELECT recipient,
              MAX(bounce_type) AS bounce_type,
              MAX(bounce_message) AS bounce_message,
              MAX(event_at) AS bounced_at,
              COUNT(*) AS count
         FROM email_events
        WHERE event_type = 'bounced' AND bounce_type = 'hard'
        GROUP BY recipient
        ORDER BY bounced_at DESC
        LIMIT ?`
    )
    .all(limit) as BounceRow[];
}

export function getSequenceHeatmap(): SequenceCell[] {
  const ev = tryOpenDb("email_events");
  const email = openDb("email");
  if (!ev) {
    return email
      .prepare(
        `SELECT sequence, day,
                COUNT(*) AS sent,
                SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS delivered,
                0 AS opened, 0 AS clicked
           FROM scheduled_sends
           WHERE sent_at IS NOT NULL
           GROUP BY sequence, day
           ORDER BY sequence, day`
      )
      .all() as SequenceCell[];
  }
  // Join via resend_id → email_events. ev attaches as a separate file via ATTACH.
  email.exec(`ATTACH DATABASE '${ev.name}' AS ev`);
  try {
    const rows = email
      .prepare(
        `SELECT s.sequence AS sequence, s.day AS day,
                SUM(CASE WHEN s.sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN ee.event_type='delivered' THEN 1 ELSE 0 END) AS delivered,
                SUM(CASE WHEN ee.event_type='opened' THEN 1 ELSE 0 END) AS opened,
                SUM(CASE WHEN ee.event_type='clicked' THEN 1 ELSE 0 END) AS clicked
           FROM scheduled_sends s
           LEFT JOIN ev.email_events ee ON ee.resend_email_id = s.resend_id
          WHERE s.sequence IS NOT NULL
          GROUP BY s.sequence, s.day
          ORDER BY s.sequence, s.day`
      )
      .all() as SequenceCell[];
    return rows;
  } finally {
    try { email.exec("DETACH DATABASE ev"); } catch { /* ignore */ }
  }
}

export function getExitReasons(): ExitReasonRow[] {
  const email = openDb("email");
  return email
    .prepare(
      "SELECT exit_reason, COUNT(*) AS count FROM subscribers WHERE trigger_type = 'cancellation' GROUP BY exit_reason ORDER BY count DESC"
    )
    .all() as ExitReasonRow[];
}

// ─── leadership ──────────────────────────────────────────────────────

export function listLeadershipMentions(opts: {
  acknowledged?: boolean;
  mentionedId?: string;
  limit?: number;
}): LeadershipMention[] {
  const ops = tryOpenDb("ops");
  if (!ops) return [];
  const conditions: string[] = [];
  const params: (string | number)[] = [];
  if (typeof opts.acknowledged === "boolean") {
    conditions.push("acknowledged = ?");
    params.push(opts.acknowledged ? 1 : 0);
  }
  if (opts.mentionedId) {
    conditions.push("mentioned_id = ?");
    params.push(opts.mentionedId);
  }
  const where = conditions.length ? `WHERE ${conditions.join(" AND ")}` : "";
  const limit = opts.limit ?? 100;
  const rows = ops
    .prepare(
      `SELECT id, message_id, channel_id, channel_name, parent_id, thread_name,
              mentioned_id, mentioned_name, author_id, author_name, content,
              created_at, jump_url, acknowledged, ack_at
         FROM leadership_mentions
         ${where}
         ORDER BY created_at DESC
         LIMIT ?`
    )
    .all(...params, limit) as Array<
      Omit<LeadershipMention, "acknowledged"> & { acknowledged: number }
    >;
  return rows.map((r) => ({ ...r, acknowledged: !!r.acknowledged }));
}

export function ackLeadershipMention(id: number): void {
  const ops = openDb("ops", { readonly: false });
  ops
    .prepare("UPDATE leadership_mentions SET acknowledged = 1, ack_at = ? WHERE id = ?")
    .run(nowSec(), id);
}

// ─── staff performance ───────────────────────────────────────────────

export function getStaffPerformance(): StaffMember[] {
  const ops = tryOpenDb("ops");
  const staff = seniorStaff();
  const since = nowSec() - 30 * DAY;
  const sinceDay = since - (since % DAY);
  return staff.map((s) => {
    if (!ops) {
      return {
        staff_user_id: s.user_id,
        display_name: s.display_name,
        total_messages_30d: 0,
        per_channel: [],
        last_seen_at: null,
        active_days_30d: 0,
        leadership_mentions_30d: 0,
        unacked_mentions: 0,
      };
    }
    const total = (ops
      .prepare(
        "SELECT COALESCE(SUM(message_count),0) AS c FROM staff_activity_daily WHERE staff_user_id = ? AND day_epoch >= ?"
      )
      .get(s.user_id, sinceDay) as { c: number }).c;
    const perChannelRows = ops
      .prepare(
        `SELECT channel_id, SUM(message_count) AS count
           FROM staff_activity_daily
          WHERE staff_user_id = ? AND day_epoch >= ?
          GROUP BY channel_id
          ORDER BY count DESC`
      )
      .all(s.user_id, sinceDay) as { channel_id: number; count: number }[];
    const channelNameMap: Record<number, string> = {};
    if (perChannelRows.length) {
      const ids = perChannelRows.map((r) => r.channel_id);
      const placeholders = ids.map(() => "?").join(",");
      const names = ops
        .prepare(
          `SELECT DISTINCT channel_id, channel_name FROM tickets WHERE channel_id IN (${placeholders})`
        )
        .all(...ids) as { channel_id: number; channel_name: string }[];
      for (const n of names) channelNameMap[n.channel_id] = n.channel_name;
    }
    const lastSeen = (ops
      .prepare(
        "SELECT MAX(last_msg_at) AS last_seen FROM staff_activity_daily WHERE staff_user_id = ?"
      )
      .get(s.user_id) as { last_seen: number | null }).last_seen;
    const activeDays = (ops
      .prepare(
        "SELECT COUNT(DISTINCT day_epoch) AS d FROM staff_activity_daily WHERE staff_user_id = ? AND day_epoch >= ?"
      )
      .get(s.user_id, sinceDay) as { d: number }).d;
    const leadership30 = (ops
      .prepare(
        "SELECT COUNT(*) AS c FROM leadership_mentions WHERE mentioned_id = ? AND created_at >= ?"
      )
      .get(s.user_id, since) as { c: number }).c;
    const unacked = (ops
      .prepare(
        "SELECT COUNT(*) AS c FROM leadership_mentions WHERE mentioned_id = ? AND acknowledged = 0"
      )
      .get(s.user_id) as { c: number }).c;
    return {
      staff_user_id: s.user_id,
      display_name: s.display_name,
      total_messages_30d: total,
      per_channel: perChannelRows.map((r) => ({
        channel_id: r.channel_id,
        channel_name: channelNameMap[r.channel_id] ?? String(r.channel_id),
        count: r.count,
      })),
      last_seen_at: lastSeen ?? null,
      active_days_30d: activeDays,
      leadership_mentions_30d: leadership30,
      unacked_mentions: unacked,
    };
  });
}

export { staffName };
