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
  BroadcastRowV2,
  BounceRow,
  SequenceCell,
  ExitReasonRow,
  LeadershipMention,
  StaffMember,
  DeliverabilityBucket,
  DomainRow,
  LinkRow,
  HourBucket,
  DowBucket,
  SendTimeReport,
  EngagementSegment,
  SoftBounceRow,
  SuppressionLogRow,
  UnsubscribeRow,
  UnsubscribeReport,
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

function _emptyKpis(): EmailKpis {
  return {
    sent_24h: 0,
    sent: 0, delivered: 0,
    opened: 0, clicked: 0,
    unique_opened: 0, unique_clicked: 0,
    bounced: 0, hard_bounced: 0, soft_bounced: 0,
    complained: 0, unsubscribed: 0,
    delivery_delayed: 0, failed: 0,
    delivery_rate: 0, open_rate: 0, click_rate: 0, ctor: 0,
    bounce_rate: 0, hard_bounce_rate: 0,
    complaint_rate: 0, unsubscribe_rate: 0,
  };
}

function _safeRate(num: number, den: number): number {
  return den > 0 ? num / den : 0;
}

export function getEmailKpis(windowDays = 30): EmailKpis {
  const ev = tryOpenDb("email_events");
  if (!ev) return _emptyKpis();
  const since = nowSec() - windowDays * DAY;

  const counts = ev
    .prepare(
      "SELECT event_type, COUNT(*) AS c FROM email_events WHERE event_at >= ? GROUP BY event_type"
    )
    .all(since) as { event_type: string; c: number }[];
  const map: Record<string, number> = {};
  for (const r of counts) map[r.event_type] = r.c;

  // Hard / soft split inside bounced.
  const bounceSplit = ev
    .prepare(
      "SELECT COALESCE(LOWER(bounce_type), '') AS bt, COUNT(*) AS c " +
        "FROM email_events WHERE event_type = 'bounced' AND event_at >= ? " +
        "GROUP BY bt"
    )
    .all(since) as { bt: string; c: number }[];
  const hard_bounced = bounceSplit.find((r) => r.bt === "hard")?.c ?? 0;
  const soft_bounced = bounceSplit.find((r) => r.bt === "soft")?.c ?? 0;

  // Unique openers / clickers (distinct recipient).
  const uOpen = ev.prepare(
    "SELECT COUNT(DISTINCT recipient) AS c FROM email_events " +
      "WHERE event_type = 'opened' AND event_at >= ?"
  ).get(since) as { c: number };
  const uClick = ev.prepare(
    "SELECT COUNT(DISTINCT recipient) AS c FROM email_events " +
      "WHERE event_type = 'clicked' AND event_at >= ?"
  ).get(since) as { c: number };

  // Unsubscribes are in their own table.
  const unsub = ev.prepare(
    "SELECT COUNT(*) AS c FROM email_unsubscribes WHERE unsubscribed_at >= ?"
  ).get(since) as { c: number };

  // Rolling 24h send volume — fixed window, ignores the selector. Used
  // to watch deliverability + stay clear of Resend's daily send cap.
  const sent24 = ev.prepare(
    "SELECT COUNT(*) AS c FROM email_events " +
      "WHERE event_type = 'sent' AND event_at >= ?"
  ).get(nowSec() - DAY) as { c: number };

  const sent = map.sent ?? 0;
  const delivered = map.delivered ?? 0;
  const opened = map.opened ?? 0;
  const clicked = map.clicked ?? 0;
  const bounced = map.bounced ?? 0;
  const complained = map.complained ?? 0;
  const delivery_delayed = map.delivery_delayed ?? 0;
  const failed = map.failed ?? 0;
  const unique_opened = uOpen?.c ?? 0;
  const unique_clicked = uClick?.c ?? 0;
  const unsubscribed = unsub?.c ?? 0;

  return {
    sent_24h: sent24?.c ?? 0,
    sent, delivered,
    opened, clicked,
    unique_opened, unique_clicked,
    bounced, hard_bounced, soft_bounced,
    complained, unsubscribed,
    delivery_delayed, failed,
    delivery_rate: _safeRate(delivered, sent),
    open_rate: _safeRate(unique_opened, delivered),
    click_rate: _safeRate(unique_clicked, delivered),
    ctor: _safeRate(unique_clicked, unique_opened),
    bounce_rate: _safeRate(bounced, sent),
    hard_bounce_rate: _safeRate(hard_bounced, sent),
    complaint_rate: _safeRate(complained, delivered),
    unsubscribe_rate: _safeRate(unsubscribed, delivered),
  };
}

export function getDeliverabilityTrend(windowDays = 30): DeliverabilityBucket[] {
  const ev = tryOpenDb("email_events");
  if (!ev) return [];
  const since = nowSec() - windowDays * DAY;
  return ev
    .prepare(
      `SELECT date(event_at, 'unixepoch') AS day,
              SUM(CASE WHEN event_type='sent' THEN 1 ELSE 0 END) AS sent,
              SUM(CASE WHEN event_type='delivered' THEN 1 ELSE 0 END) AS delivered,
              SUM(CASE WHEN event_type='bounced' THEN 1 ELSE 0 END) AS bounced,
              SUM(CASE WHEN event_type='complained' THEN 1 ELSE 0 END) AS complained,
              SUM(CASE WHEN event_type='delivery_delayed' THEN 1 ELSE 0 END) AS delivery_delayed,
              SUM(CASE WHEN event_type='failed' THEN 1 ELSE 0 END) AS failed
         FROM email_events
        WHERE event_at >= ?
        GROUP BY day
        ORDER BY day ASC`
    )
    .all(since) as DeliverabilityBucket[];
}

export function getByDomain(windowDays = 30): DomainRow[] {
  const ev = tryOpenDb("email_events");
  if (!ev) return [];
  const since = nowSec() - windowDays * DAY;

  // Top 10 domains by delivered volume + everything else collapsed into "other".
  const rows = ev
    .prepare(
      `SELECT recipient_domain AS domain,
              COUNT(DISTINCT recipient) AS recipients,
              SUM(CASE WHEN event_type='delivered' THEN 1 ELSE 0 END) AS delivered,
              SUM(CASE WHEN event_type='sent' THEN 1 ELSE 0 END) AS sent,
              SUM(CASE WHEN event_type='bounced' THEN 1 ELSE 0 END) AS bounced,
              SUM(CASE WHEN event_type='complained' THEN 1 ELSE 0 END) AS complained
         FROM email_events
        WHERE event_at >= ? AND recipient_domain != ''
        GROUP BY recipient_domain
        ORDER BY delivered DESC, recipients DESC
        LIMIT 50`
    )
    .all(since) as Array<{
      domain: string;
      recipients: number;
      delivered: number;
      sent: number;
      bounced: number;
      complained: number;
    }>;

  // Per-domain unique openers / clickers in a second pass to keep the
  // SQL above simple. Two short prepared statements scale fine.
  const uniqOpen = ev.prepare(
    "SELECT recipient_domain AS d, COUNT(DISTINCT recipient) AS c " +
      "FROM email_events WHERE event_at >= ? AND event_type='opened' " +
      "AND recipient_domain != '' GROUP BY recipient_domain"
  ).all(since) as { d: string; c: number }[];
  const uniqClick = ev.prepare(
    "SELECT recipient_domain AS d, COUNT(DISTINCT recipient) AS c " +
      "FROM email_events WHERE event_at >= ? AND event_type='clicked' " +
      "AND recipient_domain != '' GROUP BY recipient_domain"
  ).all(since) as { d: string; c: number }[];
  const openMap = new Map(uniqOpen.map((r) => [r.d, r.c]));
  const clickMap = new Map(uniqClick.map((r) => [r.d, r.c]));

  return rows.slice(0, 10).map((r) => ({
    domain: r.domain,
    recipients: r.recipients,
    delivered: r.delivered,
    open_rate: _safeRate(openMap.get(r.domain) ?? 0, r.delivered),
    click_rate: _safeRate(clickMap.get(r.domain) ?? 0, r.delivered),
    bounce_rate: _safeRate(r.bounced, r.sent || r.delivered),
    complaint_rate: _safeRate(r.complained, r.delivered),
  }));
}

export function getTopLinks(windowDays = 30, limit = 50): LinkRow[] {
  const ev = tryOpenDb("email_events");
  if (!ev) return [];
  const since = nowSec() - windowDays * DAY;
  const rows = ev
    .prepare(
      `SELECT click_url AS url,
              COUNT(*) AS clicks,
              COUNT(DISTINCT recipient) AS unique_clickers,
              GROUP_CONCAT(DISTINCT broadcast_id) AS broadcasts
         FROM email_events
        WHERE event_type = 'clicked' AND event_at >= ?
          AND click_url IS NOT NULL AND click_url != ''
        GROUP BY click_url
        ORDER BY unique_clickers DESC, clicks DESC
        LIMIT ?`
    )
    .all(since, limit) as Array<{
      url: string;
      clicks: number;
      unique_clickers: number;
      broadcasts: string | null;
    }>;
  return rows.map((r) => ({
    url: r.url,
    clicks: r.clicks,
    unique_clickers: r.unique_clickers,
    broadcasts: (r.broadcasts ?? "")
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s.length > 0),
  }));
}

export function getSendTimes(windowDays = 30): SendTimeReport {
  const ev = tryOpenDb("email_events");
  if (!ev) return { hours: [], dow: [] };
  const since = nowSec() - windowDays * DAY;

  // Group by hour-of-day (UTC). Use strftime so the bucket is the
  // delivery hour, not the open hour — open rate per delivery hour is
  // what tells you when to send.
  const hourRows = ev
    .prepare(
      `SELECT
          CAST(strftime('%H', event_at, 'unixepoch') AS INTEGER) AS hour,
          SUM(CASE WHEN event_type='delivered' THEN 1 ELSE 0 END) AS delivered,
          SUM(CASE WHEN event_type='opened' THEN 1 ELSE 0 END) AS opened
        FROM email_events
       WHERE event_at >= ?
       GROUP BY hour
       ORDER BY hour ASC`
    )
    .all(since) as { hour: number; delivered: number; opened: number }[];
  const hours: HourBucket[] = [];
  for (let h = 0; h < 24; h++) {
    const row = hourRows.find((r) => r.hour === h);
    const delivered = row?.delivered ?? 0;
    const opened = row?.opened ?? 0;
    hours.push({
      hour: h,
      delivered,
      opened,
      open_rate: _safeRate(opened, delivered),
    });
  }

  const dowRows = ev
    .prepare(
      `SELECT
          CAST(strftime('%w', event_at, 'unixepoch') AS INTEGER) AS dow,
          SUM(CASE WHEN event_type='delivered' THEN 1 ELSE 0 END) AS delivered,
          SUM(CASE WHEN event_type='opened' THEN 1 ELSE 0 END) AS opened
        FROM email_events
       WHERE event_at >= ?
       GROUP BY dow
       ORDER BY dow ASC`
    )
    .all(since) as { dow: number; delivered: number; opened: number }[];
  const dow: DowBucket[] = [];
  for (let d = 0; d < 7; d++) {
    const row = dowRows.find((r) => r.dow === d);
    const delivered = row?.delivered ?? 0;
    const opened = row?.opened ?? 0;
    dow.push({
      dow: d,
      delivered,
      opened,
      open_rate: _safeRate(opened, delivered),
    });
  }

  return { hours, dow };
}

export function getEngagementSegments(): EngagementSegment[] {
  const ev = tryOpenDb("email_events");
  if (!ev) return [];
  const now = nowSec();
  const d30 = now - 30 * DAY;
  const d90 = now - 90 * DAY;

  // Distinct recipients we've ever delivered to + their last-open timestamp.
  const rows = ev
    .prepare(
      `SELECT recipient,
              MAX(CASE WHEN event_type='opened' THEN event_at ELSE NULL END) AS last_open
         FROM email_events
        WHERE event_type IN ('delivered', 'opened')
        GROUP BY recipient`
    )
    .all() as { recipient: string; last_open: number | null }[];

  let active = 0, lapsed = 0, inactive = 0, never = 0;
  for (const r of rows) {
    if (r.last_open == null) {
      never++;
    } else if (r.last_open >= d30) {
      active++;
    } else if (r.last_open >= d90) {
      lapsed++;
    } else {
      inactive++;
    }
  }
  const total = rows.length || 1;
  return [
    { segment: "active", recipients: active, pct_of_list: active / total },
    { segment: "lapsed", recipients: lapsed, pct_of_list: lapsed / total },
    { segment: "inactive", recipients: inactive, pct_of_list: inactive / total },
    { segment: "never_opened", recipients: never, pct_of_list: never / total },
  ];
}

export function getSoftBounces(windowDays = 30, limit = 100): SoftBounceRow[] {
  const ev = tryOpenDb("email_events");
  if (!ev) return [];
  const since = nowSec() - windowDays * DAY;
  return ev
    .prepare(
      `SELECT recipient,
              COUNT(*) AS count,
              MAX(event_at) AS last_bounced_at,
              MAX(bounce_message) AS last_message
         FROM email_events
        WHERE event_type = 'bounced'
          AND LOWER(COALESCE(bounce_type, '')) = 'soft'
          AND event_at >= ?
        GROUP BY recipient
        ORDER BY count DESC, last_bounced_at DESC
        LIMIT ?`
    )
    .all(since, limit) as SoftBounceRow[];
}

export function getSuppressionLog(limit = 200): SuppressionLogRow[] {
  const whop = tryOpenDb("whop_members");
  if (!whop) return [];
  return whop
    .prepare(
      `SELECT email,
              suppressed_at,
              suppressed_reason
         FROM whop_members
        WHERE valid = 0 AND suppressed_at > 0 AND email != ''
        ORDER BY suppressed_at DESC
        LIMIT ?`
    )
    .all(limit) as SuppressionLogRow[];
}

export function getUnsubscribes(windowDays = 30): UnsubscribeReport {
  const ev = tryOpenDb("email_events");
  if (!ev) return { trend: [], recent: [], total_30d: 0 };
  const since = nowSec() - windowDays * DAY;

  const trend = ev
    .prepare(
      `SELECT date(unsubscribed_at, 'unixepoch') AS day,
              COUNT(*) AS count
         FROM email_unsubscribes
        WHERE unsubscribed_at >= ?
        GROUP BY day
        ORDER BY day ASC`
    )
    .all(since) as { day: string; count: number }[];

  const recent = ev
    .prepare(
      `SELECT recipient, source, unsubscribed_at
         FROM email_unsubscribes
        ORDER BY unsubscribed_at DESC
        LIMIT 50`
    )
    .all() as UnsubscribeRow[];

  const total = ev
    .prepare(
      "SELECT COUNT(*) AS c FROM email_unsubscribes WHERE unsubscribed_at >= ?"
    )
    .get(since) as { c: number };

  return { trend, recent, total_30d: total?.c ?? 0 };
}

export function getBroadcastsV2(): BroadcastRowV2[] {
  const ev = tryOpenDb("email_events");
  if (!ev) return [];
  const rows = ev
    .prepare(
      `SELECT broadcast_id,
              SUM(CASE WHEN event_type='sent' THEN 1 ELSE 0 END) AS sent,
              SUM(CASE WHEN event_type='delivered' THEN 1 ELSE 0 END) AS delivered,
              SUM(CASE WHEN event_type='opened' THEN 1 ELSE 0 END) AS opened,
              SUM(CASE WHEN event_type='clicked' THEN 1 ELSE 0 END) AS clicked,
              SUM(CASE WHEN event_type='bounced' THEN 1 ELSE 0 END) AS bounced,
              SUM(CASE WHEN event_type='complained' THEN 1 ELSE 0 END) AS complained
         FROM email_events
        WHERE broadcast_id IS NOT NULL AND broadcast_id != ''
        GROUP BY broadcast_id
        ORDER BY MAX(event_at) DESC
        LIMIT 50`
    )
    .all() as Array<{
      broadcast_id: string;
      sent: number;
      delivered: number;
      opened: number;
      clicked: number;
      bounced: number;
      complained: number;
    }>;
  return rows.map((r) => ({
    broadcast_id: r.broadcast_id,
    sent: r.sent,
    delivered: r.delivered,
    opened: r.opened,
    clicked: r.clicked,
    bounced: r.bounced,
    complained: r.complained,
    delivery_rate: _safeRate(r.delivered, r.sent),
    open_rate: _safeRate(r.opened, r.delivered),
    click_rate: _safeRate(r.clicked, r.delivered),
    ctor: _safeRate(r.clicked, r.opened),
    bounce_rate: _safeRate(r.bounced, r.sent),
    complaint_rate: _safeRate(r.complained, r.delivered),
  }));
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
