"""Manually insert or update a row in data/whop_members.db.

Used to add contacts to the email broadcast audience (feature-launch blasts,
channel feeler, inactivity reengagement) without them going through the
normal Whop API sync. Downstream consumers read via
`list_valid_with_email()` which filters `valid = 1 AND email != ''`; this
script writes exactly that shape.

Synthetic `whop_user_id` values (e.g. `manual_...`) won't collide with real
Whop IDs, so the 24h sync will never clobber the row.

Usage:

    python -m scripts.add_whop_member \\
        --email max@stratosphere.vip \\
        --whop-id manual_max_stratosphere

    python -m scripts.add_whop_member \\
        --email someone@example.com \\
        --whop-id manual_someone \\
        --discord-id 123456789

The script is idempotent. Running it again updates the existing row rather
than failing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running as `python scripts/add_whop_member.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.automations.whop_members_db import WhopMembersDB


async def _run(args: argparse.Namespace) -> int:
    db = WhopMembersDB(db_path=args.db_path)
    await db.open()
    try:
        await db.upsert_member(
            whop_user_id=args.whop_id,
            discord_user_id=args.discord_id,
            email=args.email,
            valid=True,
            membership_id=args.membership_id,
        )
        print(f"upserted {args.email} as {args.whop_id}")
        # Sanity-check counts after the write
        total = await db.count_valid()
        with_email = await db.count_with_email()
        print(f"whop_members now has {total} valid rows ({with_email} with email)")
    finally:
        await db.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--email", required=True,
        help="Email address to add to the broadcast list.",
    )
    parser.add_argument(
        "--whop-id", required=True,
        help=(
            "Primary key for the row. Use a synthetic prefix like "
            "'manual_' for rows that don't correspond to a real Whop user."
        ),
    )
    parser.add_argument(
        "--discord-id", default="",
        help=(
            "Optional Discord user ID if known. Leave blank for email-only "
            "recipients (they'll be skipped by activity-joined automations "
            "like the inactivity detector)."
        ),
    )
    parser.add_argument(
        "--membership-id", default="",
        help="Optional Whop membership ID. Only used for promo code minting.",
    )
    parser.add_argument(
        "--db-path", default="data/whop_members.db",
        help="Path to the whop_members SQLite file.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
