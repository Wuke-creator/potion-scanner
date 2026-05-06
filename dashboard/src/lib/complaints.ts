// Keyword-based complaint detection for captured Discord messages.
//
// The bot captures every non-bot message in #general / #alpha and every
// thread message under the need-support forum. need-support threads are
// always treated as complaints (the channel exists for that purpose).
// For general/alpha, we filter on keyword signals so the panel doesn't
// drown in trading chatter.
//
// Keyword sets are layered by severity. Each match boosts the score; the
// final severity is the highest tier that fired.

export type ComplaintSeverity = "none" | "low" | "medium" | "high";

// HIGH: explicit money-loss / fraud / refund language. These should
// always page someone.
const HIGH = [
  /\brefund\b/i,
  /\bscam(med|ming)?\b/i,
  /\bhack(ed|ing)?\b/i,
  /\bstolen\b/i,
  /\blost (my )?(money|funds|sol|btc|eth|crypto)\b/i,
  /\bfraud\b/i,
  /\brugged\b/i,
  /\bdrain(ed)?\b/i,
  /\bemergency\b/i,
  /\burgent\b/i,
  /\bphishing\b/i,
  /\bcompromised\b/i,
];

// MEDIUM: things are broken but no money loss claimed.
const MEDIUM = [
  /\b(doesn'?t|wont|won'?t|can'?t|cannot|isn'?t|aren'?t) work(ing)?\b/i,
  /\bbroken\b/i,
  /\bnot working\b/i,
  /\berror\b/i,
  /\bcrash(ed|ing)?\b/i,
  /\bfail(ed|ing)?\b/i,
  /\bbug\b/i,
  /\bglitch\b/i,
  /\bstuck\b/i,
  /\bfrustrat(ed|ing)\b/i,
  /\bridiculous\b/i,
  /\bdisappoint(ed|ing)?\b/i,
  /\bunacceptable\b/i,
  /\bcomplain(t|ing|ed)?\b/i,
  /\bcancel( my (sub|membership))?\b/i,
  /\bunsubscribe\b/i,
  /\bdidn'?t (get|receive|work)\b/i,
  /\bmissing\b/i,
  /\bwhere'?s my\b/i,
  /\b(why|how come).{0,30}(charged|charge|billed|debited)\b/i,
];

// LOW: someone is asking for help. Could be a real complaint or just a
// question — surfaced so staff can triage.
const LOW = [
  /\b(please )?help( me)?\b/i,
  /\bsupport\b/i,
  /\bissue\b/i,
  /\bproblem\b/i,
  /\bquestion\b/i,
  /\bconfused\b/i,
  /\bneed (help|assistance)\b/i,
  /\bhow do i\b/i,
  /\b@(team|staff|mod|admin|support)\b/i,
];

export function classifyComplaint(content: string): ComplaintSeverity {
  if (!content) return "none";
  if (HIGH.some((rx) => rx.test(content))) return "high";
  if (MEDIUM.some((rx) => rx.test(content))) return "medium";
  if (LOW.some((rx) => rx.test(content))) return "low";
  return "none";
}

// Returns true if a captured message should be displayed in the Complaints
// panel. need-support threads always count; general/alpha pass only when
// the keyword classifier fires above 'none'.
export function isComplaint(
  source: "ticket" | "general" | "alpha",
  content: string
): boolean {
  if (source === "ticket") return true;
  return classifyComplaint(content) !== "none";
}
