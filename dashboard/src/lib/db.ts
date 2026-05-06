// SQLite read/write wrapper. Opens each bot DB in the same Node process,
// caching the Database handles between requests. All bot DBs are opened
// readonly except ops.db which the dashboard mutates (status, notes,
// acknowledged) — kept rw on its own handle.
//
// fileMustExist is intentionally false for ops.db so the dashboard can
// boot before the bot has captured anything. Other DBs MUST exist —
// this dashboard has no purpose without the bot's data.

import Database from "better-sqlite3";
import path from "node:path";
import fs from "node:fs";

export type DbName =
  | "verified"
  | "analytics"
  | "activity"
  | "email"
  | "email_events"
  | "whop_members"
  | "cancel_survey"
  | "ops";

const FILE_NAMES: Record<DbName, string> = {
  verified: "verified.db",
  analytics: "analytics.db",
  activity: "activity.db",
  email: "email.db",
  email_events: "email_events.db",
  whop_members: "whop_members.db",
  cancel_survey: "cancel_survey_dms.db",
  ops: "ops.db",
};

const handles: Partial<Record<DbName, Database.Database>> = {};

function dataDir(): string {
  // POTION_DATA_DIR overrides; default to ../data relative to the
  // dashboard folder which works for both local dev (cwd =
  // potion-signals-bot/dashboard/) and Railway (cwd = /app/dashboard, data
  // mounted at /app/data).
  const env = process.env.POTION_DATA_DIR;
  if (env && env.trim()) return path.resolve(env.trim());
  return path.resolve(process.cwd(), "..", "data");
}

// Mirrors the schema in src/ops/db.py on the bot side. Both sides use
// CREATE ... IF NOT EXISTS so whichever runs first wins, and the other
// is a no-op. The dashboard applies this on first open so it can boot
// cleanly before OPS_CAPTURE_ENABLED is flipped on the bot.
const OPS_SCHEMA = `
CREATE TABLE IF NOT EXISTS tickets (
  message_id        INTEGER PRIMARY KEY,
  channel_id        INTEGER NOT NULL,
  channel_name      TEXT NOT NULL,
  parent_id         INTEGER,
  thread_name       TEXT,
  source            TEXT NOT NULL,
  author_id         TEXT NOT NULL,
  author_name       TEXT NOT NULL,
  author_is_staff   INTEGER DEFAULT 0,
  content           TEXT NOT NULL,
  created_at        INTEGER NOT NULL,
  captured_at       INTEGER NOT NULL,
  status            TEXT NOT NULL DEFAULT 'open',
  staff_notes       TEXT NOT NULL DEFAULT '',
  last_action_at    INTEGER,
  jump_url          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tickets_status_created ON tickets (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_channel_created ON tickets (channel_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_parent_created ON tickets (parent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_author_created ON tickets (author_id, created_at DESC);

CREATE TABLE IF NOT EXISTS leadership_mentions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id      INTEGER NOT NULL,
  channel_id      INTEGER NOT NULL,
  channel_name    TEXT NOT NULL,
  parent_id       INTEGER,
  thread_name     TEXT,
  mentioned_id    TEXT NOT NULL,
  mentioned_name  TEXT NOT NULL,
  author_id       TEXT NOT NULL,
  author_name     TEXT NOT NULL,
  content         TEXT NOT NULL,
  created_at      INTEGER NOT NULL,
  captured_at     INTEGER NOT NULL,
  jump_url        TEXT NOT NULL DEFAULT '',
  acknowledged    INTEGER NOT NULL DEFAULT 0,
  ack_at          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_leadership_recent ON leadership_mentions (acknowledged, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_leadership_mentioned ON leadership_mentions (mentioned_id, created_at DESC);

CREATE TABLE IF NOT EXISTS staff_activity_daily (
  staff_user_id   TEXT NOT NULL,
  day_epoch       INTEGER NOT NULL,
  channel_id      INTEGER NOT NULL,
  message_count   INTEGER NOT NULL DEFAULT 0,
  first_msg_at    INTEGER NOT NULL,
  last_msg_at     INTEGER NOT NULL,
  PRIMARY KEY (staff_user_id, day_epoch, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_staff_activity_day ON staff_activity_daily (day_epoch, staff_user_id);
`;

export function openDb(name: DbName, opts: { readonly?: boolean } = {}): Database.Database {
  const cached = handles[name];
  if (cached) return cached;
  const filePath = path.join(dataDir(), FILE_NAMES[name]);
  const readonly = opts.readonly ?? name !== "ops";
  // Ops DB is allowed to be missing on first boot. Everything else must exist.
  const fileMustExist = name !== "ops";
  if (fileMustExist && !fs.existsSync(filePath)) {
    throw new Error(`SQLite file not found: ${filePath}. Set POTION_DATA_DIR or run the bot first.`);
  }
  const db = new Database(filePath, { readonly, fileMustExist });
  if (!readonly) db.pragma("journal_mode = WAL");
  if (name === "ops" && !readonly) {
    db.exec(OPS_SCHEMA);
  }
  handles[name] = db;
  return db;
}

export function tryOpenDb(name: DbName): Database.Database | null {
  try {
    return openDb(name);
  } catch {
    return null;
  }
}

export function closeAll(): void {
  for (const [k, v] of Object.entries(handles)) {
    try {
      v?.close();
    } catch {
      /* ignore */
    }
    delete (handles as Record<string, Database.Database | undefined>)[k];
  }
}

// In dev, Next's HMR reloads the route module without the surrounding
// process restarting. We don't try to be clever with hot reloading; the
// handles cache lives at module scope so HMR picks up the same instances.
