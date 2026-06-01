import argparse
import asyncio
from dataclasses import dataclass
import logging
import os
import sys

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from supabase import create_client, Client

import db_writer
import discovery
from fetcher import fetch_store_data
from signals import generate_base_signals, generate_derived_signals

# ── Setup ────────────────────────────────────────────────────────────────────

load_dotenv(os.path.join(os.path.dirname(__file__), '../backend/.env'))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger("scraper")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

VALID_NICHES = ["all", "Beauty", "Fashion", "Fitness", "Jewelry", "Pets", "Home", "Electronics", "Sports"]

VALID_COUNTRIES = ["all", "USA", "UK", "Canada", "Australia", "India"]

@dataclass
class ActiveSignal:
    id: str
    slug: str
    type: str


def load_active_signals(supabase: Client) -> tuple[list[ActiveSignal], list[ActiveSignal]]:
    res = (
        supabase.table("signals")
        .select("id,slug,type,is_active")
        .eq("is_active", True)
        .execute()
    )

    base_signals: list[ActiveSignal] = []
    derived_signals: list[ActiveSignal] = []
    for row in res.data or []:
        signal = ActiveSignal(id=row["id"], slug=row["slug"], type=row["type"])
        if signal.type == "base":
            base_signals.append(signal)
        elif signal.type == "derived":
            derived_signals.append(signal)
    return base_signals, derived_signals


# ── Main async runner ────────────────────────────────────────────────────────

async def run_scraper(
    niche: str,
    country: str,
    signal: str | None,
    all_signals: bool,
    limit: int,
    dry_run: bool,
    concurrency: int,
) -> None:
    skip_signal_filter = all_signals or (signal == "all")

    # ── Discovery ──────────────────────────────────────────────────────
    logger.info(f"=== PHASE 1: Discovery — niche={niche}, country={country}, limit={limit} ===")
    url_niche_country_map = []

    ALL_NICHES = ["Beauty", "Fashion", "Fitness", "Jewelry", "Pets", "Home", "Electronics", "Sports"]
    ALL_COUNTRIES = ["USA", "UK", "Canada", "Australia", "India"]

    niches = ALL_NICHES if niche == "all" else [niche]
    countries = ALL_COUNTRIES if country == "all" else [country]

    for n in niches:
        for c in countries:
            urls = discovery.discover_stores(niche=n, country=c, limit=limit)
            for u in urls:
                url_niche_country_map.append((u, n, c))

    if not url_niche_country_map:
        logger.error("No URLs discovered. Exiting.")
        sys.exit(1)

    logger.info(f"Discovered {len(url_niche_country_map)} candidate URLs")

    supabase: Client | None = None

    active_base_signals: list[ActiveSignal] = []
    active_derived_signals: list[ActiveSignal] = []

    if not dry_run:
        if not SUPABASE_URL or not SUPABASE_KEY:
            logger.error("Supabase env vars missing")
            sys.exit(1)

        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        active_base_signals, active_derived_signals = load_active_signals(supabase)

        dataset_signal = "all" if skip_signal_filter else (signal or "all")
        dataset_id = db_writer.get_or_create_dataset(
            supabase, niche=niche, country=country, signal_slug=dataset_signal
        )

        logger.info(f"Dataset ID: {dataset_id}")
    else:
        dataset_id = "dry-run"
        logger.info("DRY RUN — no database writes")

    # ── Scrape raw + compute signals ───────────────────────────────────
    logger.info("=== PHASE 2: Scrape raw data + signal engine ===")

    matched = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        sem = asyncio.Semaphore(concurrency)

        async def process(url: str, niche_for_url: str, country_for_url: str):
            nonlocal matched

            async with sem:
                raw_data = await fetch_store_data(url, browser_context=context)
                if not raw_data.get("html"):
                    logger.error("Fetch failed for URL: %s", url)
                    return

                base_signal_slugs = (
                    generate_base_signals(raw_data, active_base_signals) if not dry_run else set()
                )
                derived_signal_slugs = (
                    generate_derived_signals(base_signal_slugs, raw_data, active_derived_signals)
                    if not dry_run
                    else set()
                )
                final_signal_slugs = base_signal_slugs.union(derived_signal_slugs)

                if not skip_signal_filter and signal and signal not in final_signal_slugs:
                    return

                if not dry_run and supabase:
                    def db_write_transaction() -> int:
                        store_id = db_writer.upsert_store(supabase, raw_data, niche_for_url, country_for_url)
                        if not store_id:
                            return 0

                        db_writer.insert_raw_data(supabase, store_id, raw_data)

                        base_by_slug = {s.slug: s for s in active_base_signals}
                        derived_by_slug = {s.slug: s for s in active_derived_signals}

                        for slug in final_signal_slugs:
                            source = "base" if slug in base_by_slug else "derived"
                            signal_entry = base_by_slug.get(slug) or derived_by_slug.get(slug)
                            if signal_entry:
                                db_writer.insert_store_signal(supabase, store_id, signal_entry.id, source=source)

                        created_for_store = 0
                        if skip_signal_filter:
                            for slug in final_signal_slugs:
                                signal_entry = base_by_slug.get(slug) or derived_by_slug.get(slug)
                                if not signal_entry:
                                    continue
                                signal_id = signal_entry.id
                                lead_id = db_writer.insert_lead(supabase, store_id, signal_id)
                                if lead_id and db_writer.link_lead_to_dataset(supabase, dataset_id, lead_id):
                                    created_for_store += 1
                                    logger.info(f"✓ Match: {slug}")
                        else:
                            selected = base_by_slug.get(signal or "") or derived_by_slug.get(signal or "")
                            if selected:
                                signal_id = selected.id
                                lead_id = db_writer.insert_lead(supabase, store_id, signal_id)
                                if lead_id and db_writer.link_lead_to_dataset(supabase, dataset_id, lead_id):
                                    created_for_store = 1
                                    logger.info(f"✓ Match: {signal}")
                        return created_for_store

                    created_for_store = await asyncio.to_thread(db_write_transaction)
                    if created_for_store:
                        matched += created_for_store
                        logger.info(f"✓ Match: {url} → {created_for_store} signal lead(s)")

        tasks = [
            process(url, niche_for_url, country_for_url)
            for url, niche_for_url, country_for_url in url_niche_country_map
        ]

        await asyncio.gather(*tasks)

        await context.close()
        await browser.close()

    if not dry_run and supabase:
        await asyncio.to_thread(db_writer.update_dataset_count, supabase, dataset_id)

    logger.info(f"=== DONE — {matched} matches ===")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--niche", required=True)
    parser.add_argument("--country", required=True)
    parser.add_argument("--signal", help="Signal slug (e.g. no_email_detected)")
    parser.add_argument("--all-signals", action="store_true")

    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    if not args.all_signals and not args.signal:
        parser.error("Either --signal or --all-signals is required.")

    niche_input = args.niche.strip().lower()
    country_input = args.country.strip().lower()

    niche_map = {n.lower(): n for n in VALID_NICHES}
    country_map = {c.lower(): c for c in VALID_COUNTRIES}

    if niche_input in niche_map:
        normalized_niche = niche_map[niche_input]
    else:
        normalized_niche = args.niche.strip().title()

    if country_input in country_map:
        normalized_country = country_map[country_input]
    else:
        if country_input in ["usa", "uk"]:
            normalized_country = country_input.upper()
        else:
            normalized_country = args.country.strip().title()

    asyncio.run(
        run_scraper(
            niche=normalized_niche,
            country=normalized_country,
            signal=args.signal,
            all_signals=args.all_signals,
            limit=args.limit,
            dry_run=args.dry_run,
            concurrency=args.concurrency,
        )
    )


if __name__ == "__main__":
    main()