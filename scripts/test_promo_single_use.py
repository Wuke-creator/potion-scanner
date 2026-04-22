"""End-to-end check that ``create_one_time_promo`` mints codes Whop will
enforce as single-use.

The check:
  1. Call ``create_one_time_promo`` from ``src/whop_api.py`` with low-risk
     parameters (1% off, 1-day TTL, ``base_code="TESTSINGLEUSE"``)
  2. Hit Whop's ``GET /api/v2/promo_codes`` listing to fetch the code back
     by name and inspect the persisted fields
  3. Print: code string, amount_off, stock, number_of_intervals (used),
     one_per_customer, expiration_datetime, status. The PASS/FAIL verdict
     is printed at the bottom

Pass criteria (single-use is enforced by Whop iff all true):
  - ``stock == 1`` (total redemptions allowed = 1)
  - ``one_per_customer == True`` (belt-and-suspenders, also blocks the
    same buyer redeeming twice if Whop's stock counter races)
  - response includes the metadata block we sent (so the WELCOME20-XXXXXX
    code is traceable to the discord_user_id that triggered it)

Usage:
    python -m scripts.test_promo_single_use

Reads ``WHOP_PROMO_API_KEY`` and ``WHOP_COMPANY_ID`` from .env via the
existing config loader. Real API call: a real promo code WILL be created
in your Whop account. It auto-expires in 24h. If you want to clean it up
sooner, the script prints the code id and a one-line curl to delete it.

Exit codes: 0 on PASS, 1 on FAIL or transport error.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.whop_api import create_one_time_promo


WHOP_API_BASE = "https://api.whop.com"


async def _list_promo_by_code(api_key: str, company_id: str, code: str) -> dict | None:
    """Fetch the just-created promo back via Whop's listing endpoint so we
    can inspect what got persisted. Returns the matching record dict, or
    None if the code can't be found."""
    timeout = aiohttp.ClientTimeout(total=10)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    # Whop v2 supports filtering the list by company; we then match by code
    # client-side because the docs don't promise a code= filter.
    url = (
        f"{WHOP_API_BASE}/api/v2/promo_codes"
        f"?company_id={company_id}&per=100"
    )
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
        async with s.get(url) as resp:
            if resp.status >= 400:
                body = await resp.text()
                print(f"GET /promo_codes failed {resp.status}: {body[:200]}")
                return None
            data = await resp.json()
    items = (
        data.get("data")
        or data.get("promo_codes")
        or (data if isinstance(data, list) else [])
    )
    for item in items:
        if str(item.get("code", "")).upper() == code.upper():
            return item
    return None


async def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    api_key = os.environ.get("WHOP_PROMO_API_KEY", "")
    company_id = os.environ.get("WHOP_COMPANY_ID", "")
    if not api_key:
        print("WHOP_PROMO_API_KEY missing from .env")
        return 1
    if not company_id:
        print("WHOP_COMPANY_ID missing from .env")
        return 1

    print(f"Company:   {company_id}")
    print("Minting a single-use test promo (TESTSINGLEUSE-XXXXXX, 1% off, 1-day TTL)")
    code = await create_one_time_promo(
        api_key=api_key,
        company_id=company_id,
        base_code="TESTSINGLEUSE",
        amount_off=1.0,
        duration_months=1,
        discord_user_id="test_user_0000",
        reason_tag="unit_test",
        ttl_days=1,
    )
    if not code:
        print("FAIL: create_one_time_promo returned None (Whop rejected the request)")
        return 1
    print(f"Created:   {code}")

    # Whop list endpoints are sometimes a beat behind on freshly-created
    # objects; brief retry loop.
    record: dict | None = None
    for attempt in range(5):
        record = await _list_promo_by_code(api_key, company_id, code)
        if record:
            break
        await asyncio.sleep(1.0)
    if not record:
        print("FAIL: created code never appeared in /promo_codes listing")
        return 1

    print("\nWhop returned these fields for the code:")
    interesting = (
        "id", "code", "amount_off", "promo_type", "base_currency",
        "stock", "number_of_intervals", "one_per_customer",
        "new_users_only", "promo_duration_months", "expiration_datetime",
        "status", "metadata",
    )
    for k in interesting:
        if k in record:
            print(f"  {k}: {record[k]!r}")

    # Verdict
    stock = record.get("stock")
    one_per_customer = record.get("one_per_customer")
    metadata = record.get("metadata") or {}

    pass_stock = stock == 1
    pass_one_per_customer = bool(one_per_customer)
    pass_metadata = (
        metadata.get("source") == "cancel_survey_dm"
        and metadata.get("discord_user_id") == "test_user_0000"
    )

    print("\nVerdict:")
    print(f"  stock == 1 .................. {'PASS' if pass_stock else 'FAIL'} (got {stock!r})")
    print(f"  one_per_customer == True .... {'PASS' if pass_one_per_customer else 'FAIL'} (got {one_per_customer!r})")
    print(f"  metadata round-trip ......... {'PASS' if pass_metadata else 'FAIL'}")

    promo_id = record.get("id")
    if promo_id:
        print(
            f"\nTo delete the test code now (it auto-expires in 24h either way):\n"
            f"  curl -X DELETE -H 'Authorization: Bearer $WHOP_PROMO_API_KEY' "
            f"{WHOP_API_BASE}/api/v2/promo_codes/{promo_id}"
        )

    overall = pass_stock and pass_one_per_customer and pass_metadata
    print(f"\nOverall: {'PASS' if overall else 'FAIL'}")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
