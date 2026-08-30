"""Offline re-scoring over stored fingerprints.

Replaces `recalculate.py`, which was a net-negative script: it read HTML that had
been run through a cleaner stripping every `<script>` tag (so it saw no vendors
at all), passed `{}` for subpages (so it fabricated four content signals for
every store at 0.90+ confidence), and **deleted the store's existing signals
first**. Running it degraded the database.

This version re-runs the current detectors over stored fingerprints. No network,
no SerpAPI spend, fully replayable — which is the entire point of persisting
fingerprints instead of HTML.

    python rescore.py --dry-run          # report what would change
    python rescore.py --apply            # append new observations
"""

import argparse
import logging
import os
import sys
from typing import Any, Optional

from dotenv import load_dotenv
from supabase import Client, create_client

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db_writer  # noqa: E402
from config import DETECTOR_VERSION  # noqa: E402
from maturity import score_maturity  # noqa: E402
from signals import generate_signals, to_usd  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "../backend/.env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger("rescore")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")


def latest_observations(supabase: Client, batch: int = 500) -> list[dict[str, Any]]:
    """Most recent observation per store."""
    rows: list[dict[str, Any]] = []
    page = 0
    while True:
        res = (supabase.table("store_observations")
               .select("id,store_id,fingerprint,fetch_quality,signals,observed_at,detector_version")
               .order("observed_at", desc=True)
               .range(page * batch, (page + 1) * batch - 1)
               .execute())
        chunk = res.data or []
        rows.extend(chunk)
        if len(chunk) < batch:
            break
        page += 1

    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["store_id"] not in latest:
            latest[row["store_id"]] = row
    return list(latest.values())


def run(apply_changes: bool, only_stale_version: bool) -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("Supabase env vars missing")
        return

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    observations = latest_observations(supabase)
    logger.info("Re-scoring %s stores at detector_version=%s", len(observations), DETECTOR_VERSION)

    changed = unchanged = skipped = 0

    for observation in observations:
        if only_stale_version and observation.get("detector_version") == DETECTOR_VERSION:
            skipped += 1
            continue

        fingerprint = observation.get("fingerprint")
        if not fingerprint:
            skipped += 1
            continue

        quality = observation.get("fetch_quality") or {}
        signals, attributes = generate_signals(fingerprint, quality)

        before = set(observation.get("signals") or [])
        after = set(signals.keys())
        if before == after:
            unchanged += 1
            continue

        changed += 1
        logger.info(
            "store=%s  +%s  -%s",
            observation["store_id"],
            sorted(after - before) or "-",
            sorted(before - after) or "-",
        )

        if apply_changes:
            avg_price_usd = to_usd(attributes.get("avg_price"), attributes.get("currency"))
            maturity = score_maturity(attributes, fingerprint.get("product_count"), avg_price_usd)
            # Append, never delete — history is the product.
            db_writer.insert_observation(
                supabase, observation["store_id"], fingerprint, signals, attributes, quality,
            )
            supabase.table("stores").update({
                "maturity_tier": maturity["tier"],
                "maturity_score": maturity["score"],
            }).eq("id", observation["store_id"]).execute()

    logger.info(
        "Done — changed=%s unchanged=%s skipped=%s (%s)",
        changed, unchanged, skipped,
        "applied" if apply_changes else "dry run, nothing written",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="report changes without writing")
    group.add_argument("--apply", action="store_true", help="append re-scored observations")
    parser.add_argument("--only-stale-version", action="store_true",
                        help=f"skip stores already scored at {DETECTOR_VERSION}")
    args = parser.parse_args()
    run(apply_changes=args.apply, only_stale_version=args.only_stale_version)


if __name__ == "__main__":
    main()
