"""Export every valid whop_members row with an email to one or more CSVs
that Resend's "Import contacts" dashboard flow accepts directly.

Usage:

    # one CSV with everything
    python -m scripts.export_resend_audience_csv

    # split into 4 evenly-sized CSVs, freshest synced first
    python -m scripts.export_resend_audience_csv --batches 4

    # custom output stem and dedupe against in-flight sequence sends
    python -m scripts.export_resend_audience_csv \\
        --out data/audience.csv --batches 4 \\
        --dedupe-against data/email.db

CSV format matches Resend's importer: one row per contact, columns
`email,first_name,last_name,unsubscribed`. We don't have first/last name
in whop_members, so those columns are blank. Resend treats blank
first_name as a missing merge field and falls back to whatever default
you set in the broadcast template.

When `--batches N > 1`, the script writes N files named
`<stem>_1of<N>.csv`, `<stem>_2of<N>.csv`, etc. Rows are split in source
order (whop_members returns freshest-synced first), so batch 1 is the
most recently active addresses and batch N is the stalest. Send batch 1
first, watch your bounce + complaint rate in the Resend dashboard, then
proceed to the next batch only if both stay healthy
(bounce < 2%, complaints < 0.1%).

The `--dedupe-against` flag skips any address already in the email_bot
sequence DB (so people currently in a winback drip don't also get the
one-shot blast). Optional but recommended.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.automations.whop_members_db import WhopMembersDB


def _load_dedupe_set(email_db_path: str) -> set[str]:
    """Pull every email currently mid-sequence in email.db so we don't
    double-send. Returns lowercased set, empty on error or missing table."""
    if not Path(email_db_path).exists():
        return set()
    try:
        conn = sqlite3.connect(email_db_path)
        try:
            rows = conn.execute(
                "SELECT DISTINCT email FROM scheduled_sends "
                "WHERE status = 'pending'"
            ).fetchall()
        except sqlite3.OperationalError:
            return set()
        finally:
            conn.close()
        return {(r[0] or "").strip().lower() for r in rows if r[0]}
    except Exception:
        return set()


def _batch_paths(out: Path, batches: int) -> list[Path]:
    """For batches=1 returns [out]. For batches=N returns
    [out_with_stem_1ofN, ..., out_with_stem_NofN]."""
    if batches <= 1:
        return [out]
    return [
        out.with_name(f"{out.stem}_{i}of{batches}{out.suffix}")
        for i in range(1, batches + 1)
    ]


async def _run(args: argparse.Namespace) -> int:
    if args.batches < 1:
        print("--batches must be >= 1", file=sys.stderr)
        return 2

    dedupe = _load_dedupe_set(args.dedupe_against) if args.dedupe_against else set()
    if dedupe:
        print(f"Loaded {len(dedupe)} addresses to dedupe against")

    db = WhopMembersDB(db_path=args.db_path)
    await db.open()
    try:
        rows = await db.list_valid_with_email()
    finally:
        await db.close()

    # First pass: filter + dedupe so we know the final count before splitting.
    seen: set[str] = set()
    cleaned: list[str] = []
    skipped_dupe_in_file = 0
    skipped_dupe_against_db = 0
    skipped_invalid = 0
    for r in rows:
        email = (r.email or "").strip().lower()
        if not email or "@" not in email:
            skipped_invalid += 1
            continue
        if email in seen:
            skipped_dupe_in_file += 1
            continue
        if email in dedupe:
            skipped_dupe_against_db += 1
            continue
        seen.add(email)
        cleaned.append(email)

    total = len(cleaned)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    paths = _batch_paths(out, args.batches)

    # Even split with remainder spread across the first few files so
    # batch sizes differ by at most 1.
    base, extra = divmod(total, args.batches)
    sizes = [base + (1 if i < extra else 0) for i in range(args.batches)]

    cursor = 0
    for path, size in zip(paths, sizes):
        chunk = cleaned[cursor : cursor + size]
        cursor += size
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["email", "first_name", "last_name", "unsubscribed"])
            for email in chunk:
                writer.writerow([email, "", "", "false"])
        print(f"wrote {len(chunk)} contact(s) to {path}")

    print(f"total: {total} contact(s) across {args.batches} file(s)")
    if skipped_invalid:
        print(f"  skipped {skipped_invalid} invalid email(s)")
    if skipped_dupe_in_file:
        print(f"  skipped {skipped_dupe_in_file} duplicate(s) within whop_members")
    if skipped_dupe_against_db:
        print(f"  skipped {skipped_dupe_against_db} already mid-sequence in email.db")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path", default="data/whop_members.db",
        help="Path to the whop_members SQLite file.",
    )
    parser.add_argument(
        "--out", default="data/resend_audience.csv",
        help=(
            "Output CSV path. With --batches N>1, the path is used as a stem "
            "and N files are written as <stem>_1ofN.csv ... <stem>_NofN.csv. "
            "Existing files are overwritten."
        ),
    )
    parser.add_argument(
        "--batches", type=int, default=1,
        help=(
            "Number of evenly-sized CSV files to split the audience into. "
            "Default 1 (single file). Rows are split in source order "
            "(freshest synced first) so file 1 is the most recent slice."
        ),
    )
    parser.add_argument(
        "--dedupe-against", default="",
        help=(
            "Optional path to email.db. Any address in scheduled_sends "
            "(status=pending) is excluded from the export so people currently "
            "in a drip sequence don't also get the one-shot blast."
        ),
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
