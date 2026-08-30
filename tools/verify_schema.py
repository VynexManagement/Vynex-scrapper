"""Check the database matches what the scraper expects.

Run after applying db/migrations/04_scraper_rebuild.sql, before the first live
scrape. Read-only: it writes nothing.

    python tools/verify_schema.py
"""

import os
import sys

from dotenv import load_dotenv
from supabase import create_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "../backend/.env"))

from config import SIGNAL_SLUGS  # noqa: E402

REQUIRED = {
    "stores": ["id", "domain", "url", "name", "shop_handle", "country", "niche", "status",
               "product_count", "avg_price", "avg_price_usd", "currency", "maturity_tier",
               "maturity_score", "contact_email", "contact_source", "first_seen_at",
               "last_checked_at"],
    "store_observations": ["id", "store_id", "observed_at", "fingerprint", "fingerprint_hash",
                           "signals", "signal_detail", "attributes", "fetch_quality",
                           "scraper_version", "detector_version"],
    "store_changes": ["id", "store_id", "change_type", "vendor", "detected_at"],
    "discovery_cursors": ["query_hash", "query", "next_start", "exhausted", "updated_at"],
    "sheets": ["id", "niche", "country", "signal_slugs", "query_hash", "total_rows",
               "price_usd", "description", "created_at", "last_built_at"],
    "sheet_rows": ["sheet_id", "store_id", "signals", "evidence", "angle", "created_at"],
    "signals": ["id", "slug", "name", "description", "is_active", "created_at"],
    "api_quota": ["service", "month", "used", "updated_at"],
}

GONE = ["leads", "dataset_leads", "datasets", "store_raw_data", "store_signals"]


def main() -> int:
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        print("FAIL  SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")
        return 1

    supabase = create_client(url, key)
    problems: list[str] = []

    print("-- required tables --")
    for table, columns in REQUIRED.items():
        try:
            res = supabase.table(table).select(",".join(columns)).limit(1).execute()
        except Exception as exc:
            msg = str(exc).replace("\n", " ")[:110]
            problems.append(f"{table}: {msg}")
            print(f"  FAIL  {table:<20} {msg}")
            continue
        print(f"  ok    {table:<20} {len(columns)} columns, {len(res.data or [])} row(s) sampled")

    print("\n-- dropped tables (should be gone) --")
    for table in GONE:
        try:
            supabase.table(table).select("*").limit(1).execute()
            problems.append(f"{table} still exists")
            print(f"  FAIL  {table:<20} still present")
        except Exception:
            print(f"  ok    {table:<20} gone")

    print("\n-- seeded signals --")
    try:
        res = supabase.table("signals").select("slug,is_active").execute()
        found = {r["slug"] for r in (res.data or [])}
        missing = [s for s in SIGNAL_SLUGS if s not in found]
        extra = [s for s in found if s not in SIGNAL_SLUGS]
        print(f"  {len(found)} rows; expected {len(SIGNAL_SLUGS)}")
        if missing:
            problems.append(f"signals missing: {missing}")
            print(f"  FAIL  missing: {missing}")
        if extra:
            print(f"  note  unexpected (harmless): {extra}")
        if not missing:
            print("  ok    all 8 base signals present")
    except Exception as exc:
        problems.append(f"signals: {exc}")
        print(f"  FAIL  {exc}")

    print("\n-- quota RPC --")
    try:
        # p_limit 0 makes this a no-op probe: it increments then reports over-limit.
        supabase.rpc("increment_quota", {"p_service": "_probe", "p_month": "0000-00",
                                         "p_limit": 0}).execute()
        print("  ok    increment_quota callable")
    except Exception as exc:
        problems.append(f"increment_quota: {exc}")
        print(f"  FAIL  {str(exc)[:110]}")

    print()
    if problems:
        print(f"NOT READY - {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("READY - schema matches. Safe to run the scraper without --dry-run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
