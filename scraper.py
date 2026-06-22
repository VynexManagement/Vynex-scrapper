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
from signals import generate_base_signals, generate_derived_signals, classify_niche, classify_country

# ── Setup ────────────────────────────────────────────────────────────────────

load_dotenv(os.path.join(os.path.dirname(__file__), '../backend/.env'))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger("scraper")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

from config import NICHES, COUNTRIES, STORE_FRESHNESS_DAYS

VALID_NICHES = ["all"] + NICHES
VALID_COUNTRIES = ["all"] + COUNTRIES

@dataclass
class ActiveSignal:
    id: str
    slug: str
    type: str
    dependencies: list[str] = None


def load_active_signals(supabase: Client) -> tuple[list[ActiveSignal], list[ActiveSignal]]:
    res = (
        supabase.table("signals")
        .select("id,slug,type,is_active,dependencies")
        .eq("is_active", True)
        .execute()
    )

    base_signals: list[ActiveSignal] = []
    derived_signals: list[ActiveSignal] = []
    for row in res.data or []:
        signal = ActiveSignal(
            id=row["id"],
            slug=row["slug"],
            type=row["type"],
            dependencies=row.get("dependencies") or []
        )
        if signal.type == "base":
            base_signals.append(signal)
        elif signal.type == "derived":
            derived_signals.append(signal)
    return base_signals, derived_signals


# ── Main async runner ────────────────────────────────────────────────────────

async def run_scraper(
    niche: str | None,
    country: str | None,
    signal: str | None,
    all_signals: bool,
    limit: int,
    dry_run: bool,
    concurrency: int,
    refresh_stale: bool = False,
    freshness_threshold: int = 30,
    url_arg: str | None = None,
    retry_attempts: int = 2,
) -> None:
    skip_signal_filter = all_signals or (signal == "all")

    # ── Discovery ──────────────────────────────────────────────────────
    url_niche_country_map = []

    if url_arg:
        logger.info(f"=== PHASE 1: Single URL Target Mode — url={url_arg} ===")
        url_niche_country_map.append((url_arg, niche or "Unknown", country or "Unknown"))
    elif refresh_stale:
        logger.info(f"=== PHASE 1: Refresh Stale Mode — threshold={freshness_threshold} days ===")
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("Supabase env vars missing.")
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=freshness_threshold)).isoformat()
        res1 = supabase.table("stores").select("id, url, niche, country").is_("last_scraped_at", "null").execute()
        res2 = supabase.table("stores").select("id, url, niche, country").lt("last_scraped_at", cutoff).execute()
        stale_stores = {s["id"]: s for s in (res1.data or []) + (res2.data or [])}
        for s in stale_stores.values():
            url_niche_country_map.append((s["url"], s.get("niche") or "Unknown", s.get("country") or "Unknown"))
        logger.info(f"Found {len(url_niche_country_map)} stale stores in DB.")
    else:
        logger.info(f"=== PHASE 1: Discovery — niche={niche}, country={country}, limit={limit} ===")
        if niche == "all" and country == "all":
            # Generic discovery — classify niche/country from HTML after fetching
            logger.info("=== Generic discovery mode (all niches, all countries) ===")
            raw_urls = discovery.discover_stores_generic(limit=limit)
            for u in raw_urls:
                url_niche_country_map.append((u, None, None))  # None = classify later
        else:
            niches = NICHES if niche == "all" else [niche]
            countries = COUNTRIES if country == "all" else [country]
            for n in niches:
                for c in countries:
                    urls = discovery.discover_stores(niche=n, country=c, limit=limit)
                    for u in urls:
                        url_niche_country_map.append((u, n, c))

    if not url_niche_country_map:
        logger.info("No target stores discovered or enqueued. Exiting scraper.")
        return

    # Deduplicate by URL — first occurrence wins
    seen_urls: set[str] = set()
    deduped_map = []
    for url, n, c in url_niche_country_map:
        if url not in seen_urls:
            seen_urls.add(url)
            deduped_map.append((url, n, c))
    url_niche_country_map = deduped_map
    logger.info(f"After dedup: {len(url_niche_country_map)} unique URLs")

    supabase: Client | None = None

    active_base_signals: list[ActiveSignal] = []
    active_derived_signals: list[ActiveSignal] = []

    if not dry_run:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("Supabase env vars missing.")

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
        # Load mock active signals using rules keys directly
        from signals import BASE_RULES, DERIVED_RULES
        active_base_signals = [ActiveSignal(id=f"mock-{slug}", slug=slug, type="base") for slug in BASE_RULES.keys()]
        active_derived_signals = [ActiveSignal(id=f"mock-{slug}", slug=slug, type="derived") for slug in DERIVED_RULES.keys()]

    # ── Scrape raw + compute signals ───────────────────────────────────
    logger.info("=== PHASE 2: Scrape raw data + signal engine ===")

    discovered_count = len(url_niche_country_map)
    scraped_count = 0
    skipped_count = 0
    failed_count = 0
    signals_count = 0
    leads_count = 0
    db_failures_count = 0

    db_lock = asyncio.Lock()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        sem = asyncio.Semaphore(concurrency)

        async def process(url: str, niche_for_url: str, country_for_url: str):
            nonlocal scraped_count, skipped_count, failed_count, signals_count, leads_count, db_failures_count

            async with sem:
                raw_data = await fetch_store_data(url, browser_context=context)
                if not raw_data.get("is_shopify") or not raw_data.get("html") or raw_data.get("is_dead"):
                    if raw_data.get("is_dead") or not raw_data.get("html"):
                        failed_count += 1
                        logger.info("Failed store: %s", url)
                    else:
                        skipped_count += 1
                        logger.info("Skipping non-Shopify store: %s", url)
                    
                    logger.info(
                        f"[PROGRESS] Discovered: {discovered_count} | Scraped: {scraped_count} | Skipped: {skipped_count} | "
                        f"Failed: {failed_count} | Signals: {signals_count} | Leads: {leads_count} | DB Failures: {db_failures_count}"
                    )
                    return

                # Resolve niche and country for "all" case
                resolved_niche = niche_for_url or classify_niche(raw_data) or "Unknown"
                resolved_country = country_for_url or classify_country(raw_data) or "Unknown"

                base_signal_evidences = generate_base_signals(raw_data, active_base_signals)
                base_signal_slugs = set(base_signal_evidences.keys())

                derived_signal_evidences = generate_derived_signals(base_signal_slugs, raw_data, active_derived_signals)
                
                final_signal_evidences = {**base_signal_evidences, **derived_signal_evidences}

                scraped_count += 1
                signals_count += len(final_signal_evidences)

                if not skip_signal_filter and signal and signal not in final_signal_evidences:
                    logger.info(
                        f"[PROGRESS] Discovered: {discovered_count} | Scraped: {scraped_count} | Skipped: {skipped_count} | "
                        f"Failed: {failed_count} | Signals: {signals_count} | Leads: {leads_count} | DB Failures: {db_failures_count}"
                    )
                    return

                if dry_run:
                    logger.info(f"[DRY-RUN] {url} resolved niche: {resolved_niche}, resolved country: {resolved_country}")
                    logger.info(f"[DRY-RUN] {url} attributes: {raw_data.get('attributes')}")
                    logger.info(f"[DRY-RUN] {url} matched signals: {sorted(final_signal_evidences.keys())}")
                    for slug, evidence in base_signal_evidences.items():
                        logger.info(f"  - Base Signal: {slug} | Evidence: {evidence}")
                    for slug, evidence in derived_signal_evidences.items():
                        logger.info(f"  - Derived Signal: {slug} | Evidence: {evidence}")
                    leads_count += 1
                else:
                    if supabase:
                        async with db_lock:
                            created_leads, db_fails = await asyncio.to_thread(
                                db_writer.write_store_to_db,
                                supabase, raw_data, resolved_niche, resolved_country,
                                dataset_id, final_signal_evidences,
                                active_base_signals, active_derived_signals,
                                signal, skip_signal_filter
                            )
                        leads_count += created_leads
                        db_failures_count += db_fails
                        if created_leads:
                            logger.info(f"✓ Match: {url} → {created_leads} signal lead(s)")

                logger.info(
                    f"[PROGRESS] Discovered: {discovered_count} | Scraped: {scraped_count} | Skipped: {skipped_count} | "
                    f"Failed: {failed_count} | Signals: {signals_count} | Leads: {leads_count} | DB Failures: {db_failures_count}"
                )

        tasks = [
            process(url, niche_for_url, country_for_url)
            for url, niche_for_url, country_for_url in url_niche_country_map
        ]

        await asyncio.gather(*tasks)

        await context.close()
        await browser.close()

    if not dry_run and supabase:
        await asyncio.to_thread(db_writer.update_dataset_count, supabase, dataset_id)
        await asyncio.to_thread(db_writer.touch_dataset_scraped_at, supabase, dataset_id)

    logger.info("=== FINAL RUN METRICS ===")
    logger.info(f"Discovered: {discovered_count}")
    logger.info(f"Scraped: {scraped_count}")
    logger.info(f"Skipped: {skipped_count}")
    logger.info(f"Failed: {failed_count}")
    logger.info(f"Signals: {signals_count}")
    logger.info(f"Leads: {leads_count}")
    logger.info(f"DB Failures: {db_failures_count}")
    logger.info(f"=== DONE — {leads_count} matches ===")



# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--niche")
    parser.add_argument("--country")
    parser.add_argument("--signal", help="Signal slug (e.g. no_email_detected)")
    parser.add_argument("--all-signals", action="store_true")

    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")

    # New options
    parser.add_argument("--refresh-stale", action="store_true")
    parser.add_argument("--freshness-threshold", type=int, default=30)
    parser.add_argument("--url")
    parser.add_argument("--retry-attempts", type=int, default=2)

    args = parser.parse_args()
    
    if not args.refresh_stale and not args.url:
        if not args.niche or not args.country:
            parser.error("Niche and country are required unless running with --refresh-stale or --url.")

    if not args.all_signals and not args.signal:
        parser.error("Either --signal or --all-signals is required.")

    normalized_niche = None
    normalized_country = None

    if args.niche:
        niche_input = args.niche.strip().lower()
        niche_map = {n.lower(): n for n in VALID_NICHES}
        if niche_input in niche_map:
            normalized_niche = niche_map[niche_input]
        else:
            normalized_niche = args.niche.strip().title()

    if args.country:
        country_input = args.country.strip().lower()
        country_map = {c.lower(): c for c in VALID_COUNTRIES}
        if country_input in country_map:
            normalized_country = country_map[country_input]
        else:
            if country_input in ["usa", "uk"]:
                normalized_country = country_input.upper()
            else:
                normalized_country = args.country.strip().title()

    try:
        asyncio.run(
            run_scraper(
                niche=normalized_niche,
                country=normalized_country,
                signal=args.signal,
                all_signals=args.all_signals,
                limit=args.limit,
                dry_run=args.dry_run,
                concurrency=args.concurrency,
                refresh_stale=args.refresh_stale,
                freshness_threshold=args.freshness_threshold,
                url_arg=args.url,
                retry_attempts=args.retry_attempts,
            )
        )
    except RuntimeError as e:
        logger.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()