// Senior staff list, parsed from POTION_SENIOR_STAFF_IDS env var.
// Empty list = no leadership/staff features are populated, but the rest
// of the dashboard still works.

export interface StaffEntry {
  user_id: string;
  display_name: string;
}

const FALLBACK_NAMES: Record<string, string> = {
  "901091776977338419": "Luke",
  "197740443281129472": "Swaag",
  "264434503362150400": "Lucas",
  "885442563606196264": "Nando",
};

let cached: StaffEntry[] | null = null;

export function seniorStaff(): StaffEntry[] {
  if (cached) return cached;
  const raw = (process.env.POTION_SENIOR_STAFF_IDS || "").trim();
  const ids = raw
    ? raw.split(",").map((s) => s.trim()).filter((s) => /^\d+$/.test(s))
    : Object.keys(FALLBACK_NAMES);
  cached = ids.map((id) => ({
    user_id: id,
    display_name: FALLBACK_NAMES[id] ?? `Staff ${id.slice(0, 6)}…`,
  }));
  return cached;
}

export function staffIdSet(): Set<string> {
  return new Set(seniorStaff().map((s) => s.user_id));
}

export function staffName(userId: string): string {
  const match = seniorStaff().find((s) => s.user_id === userId);
  return match?.display_name ?? `User ${userId.slice(0, 6)}…`;
}
