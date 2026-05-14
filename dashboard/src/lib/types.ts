// Types for the Potion Ops Dashboard. Mirrors the row shapes coming back
// from the SQLite reads in queries.ts so API routes and components share
// one contract.

export interface SummaryCounts {
  openTickets: number;
  ticketsTrend14d: number[];
  // Customer complaints across all 3 monitored channels: every open
  // need-support thread message + general/alpha messages flagged by the
  // keyword classifier. ``openComplaintsHigh`` is the urgent subset
  // (refund/scam/hack/etc.) so the home card can show a tone-warning.
  openComplaints: number;
  openComplaintsHigh: number;
  complaintsTrend14d: number[];
  signalsToday: number;
  signalsYesterday: number;
  verifiedActive: number;
  emailQueueDepth: number;
  bounceRate24h: number;
  bounceTotal24h: number;
  dunningActive: number;
  unackedLeadership: number;
  // Missed calls = signals that hit stop-loss or got canceled, plus user
  // complaints captured as "Caller-ticket-*" threads in the support forum.
  missedCalls7d: number;
  missedCallsTrend14d: number[];
  missedCallsBreakdown: { stop_hit: number; canceled: number; open_caller_tickets: number };
  // Last email-pipeline activity timestamp. Used by the home card + email
  // panel to render an online/stale/offline indicator. Null = no email
  // sends recorded ever (truly offline).
  lastEmailActivityAt: number | null;
}

export type TicketStatus = "open" | "in_progress" | "resolved";
export type TicketSource = "general" | "alpha" | "ticket";
export type ComplaintSeverity = "none" | "low" | "medium" | "high";

export interface TicketRow {
  message_id: number;
  channel_id: number;
  channel_name: string;
  parent_id: number | null;
  thread_name: string | null;
  source: TicketSource;
  author_id: string;
  author_name: string;
  author_is_staff: boolean;
  content: string;
  created_at: number;
  captured_at: number;
  status: TicketStatus;
  staff_notes: string;
  last_action_at: number | null;
  jump_url: string;
  // Computed at read-time by the dashboard's complaint classifier. None
  // means the message is normal chat; low/medium/high means it pinged
  // one or more complaint keywords.
  complaint_severity: ComplaintSeverity;
}

export interface ChannelStats {
  channel_key: string;
  signal_count_7d: number;
  signal_count_30d: number;
  win_count: number;
  closed_count: number;
  win_rate: number; // 0..1
  avg_pnl_pct: number;
  best_pnl_pct: number | null;
  best_trade_pair: string | null;
  best_trade_id: number | null;
  worst_pnl_pct: number | null;
  worst_trade_pair: string | null;
  worst_trade_id: number | null;
}

export interface CallerStats {
  source_discord_user_id: string;
  signal_count_7d: number;
  signal_count_30d: number;
  win_count: number;
  closed_count: number;
  win_rate: number;
  avg_pnl_pct: number;
}

export interface SignalFeedRow {
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

export interface PnLBucket {
  range: string;       // "<-50%", "-50..-25%", etc
  bucket_min: number;  // inclusive lower bound (-Infinity OK)
  bucket_max: number;  // exclusive upper bound (Infinity OK)
  count: number;
}

export interface FunnelStage {
  stage: string;
  count: number;
}

export interface QueueRow {
  sequence: string;
  pending: number;
}

export interface EmailKpis {
  // Raw counts.
  sent: number;
  delivered: number;
  opened: number;            // total open events (a recipient can fire multiple)
  clicked: number;           // total click events
  unique_opened: number;     // distinct recipients who opened at least once
  unique_clicked: number;    // distinct recipients who clicked at least once
  bounced: number;
  hard_bounced: number;
  soft_bounced: number;
  complained: number;
  unsubscribed: number;
  delivery_delayed: number;
  failed: number;
  // Industry-standard rates. All in 0..1.
  delivery_rate: number;            // delivered / sent
  open_rate: number;                // unique_opened / delivered  (a.k.a. "unique open rate")
  click_rate: number;               // unique_clicked / delivered (a.k.a. "unique click rate")
  ctor: number;                     // unique_clicked / unique_opened (click-to-open)
  bounce_rate: number;              // bounced / sent
  hard_bounce_rate: number;         // hard_bounced / sent
  complaint_rate: number;           // complained / delivered
  unsubscribe_rate: number;         // unsubscribed / delivered
}

export interface DeliverabilityBucket {
  day: string;             // YYYY-MM-DD (UTC)
  sent: number;
  delivered: number;
  bounced: number;
  complained: number;
  delivery_delayed: number;
  failed: number;
}

export interface DomainRow {
  domain: string;          // gmail.com / yahoo.com / etc.
  recipients: number;      // distinct
  delivered: number;
  open_rate: number;
  click_rate: number;
  bounce_rate: number;
  complaint_rate: number;
}

export interface LinkRow {
  url: string;
  clicks: number;
  unique_clickers: number;
  broadcasts: string[];    // broadcast_ids (empty for transactional)
}

export interface HourBucket {
  hour: number;            // 0..23 UTC
  delivered: number;
  opened: number;
  open_rate: number;       // 0..1
}

export interface DowBucket {
  dow: number;             // 0=Sunday .. 6=Saturday
  delivered: number;
  opened: number;
  open_rate: number;
}

export interface SendTimeReport {
  hours: HourBucket[];
  dow: DowBucket[];
}

export type EngagementBand =
  | "active"        // opened in last 30d
  | "lapsed"        // opened 30-90d ago
  | "inactive"      // opened >90d ago
  | "never_opened"; // ever delivered, no open ever

export interface EngagementSegment {
  segment: EngagementBand;
  recipients: number;
  pct_of_list: number;     // 0..1
}

export interface SoftBounceRow {
  recipient: string;
  count: number;           // soft bounces in last 30 days
  last_bounced_at: number; // epoch seconds
  last_message: string | null;
}

export interface SuppressionLogRow {
  email: string;
  suppressed_at: number;   // epoch seconds
  suppressed_reason: string;
}

export interface UnsubscribeRow {
  recipient: string;
  source: string;
  unsubscribed_at: number;
}

export interface UnsubscribeReport {
  trend: { day: string; count: number }[];
  recent: UnsubscribeRow[];
  total_30d: number;
}

// Per-broadcast row, extended with rates for the upgraded Broadcasts tab.
// Existing BroadcastRow stays for backward compat; UI prefers BroadcastRowV2
// when available.
export interface BroadcastRowV2 {
  broadcast_id: string;
  sent: number;
  delivered: number;
  opened: number;
  clicked: number;
  bounced: number;
  complained: number;
  delivery_rate: number;
  open_rate: number;
  click_rate: number;
  ctor: number;
  bounce_rate: number;
  complaint_rate: number;
}

export interface BroadcastRow {
  broadcast_id: string;
  delivered: number;
  opened: number;
  clicked: number;
  bounced: number;
  complained: number;
  total: number;
}

export interface BounceRow {
  recipient: string;
  bounce_type: string | null;
  bounce_message: string | null;
  bounced_at: number;
  count: number;
}

export interface SequenceCell {
  sequence: string;
  day: number;
  sent: number;
  delivered: number;
  opened: number;
  clicked: number;
}

export interface ExitReasonRow {
  exit_reason: string;
  count: number;
}

export interface LeadershipMention {
  id: number;
  message_id: number;
  channel_id: number;
  channel_name: string;
  parent_id: number | null;
  thread_name: string | null;
  mentioned_id: string;
  mentioned_name: string;
  author_id: string;
  author_name: string;
  content: string;
  created_at: number;
  jump_url: string;
  acknowledged: boolean;
  ack_at: number | null;
}

export interface StaffMember {
  staff_user_id: string;
  display_name: string;
  total_messages_30d: number;
  per_channel: { channel_id: number; channel_name: string; count: number }[];
  last_seen_at: number | null;
  active_days_30d: number;
  leadership_mentions_30d: number;
  unacked_mentions: number;
}
