"""Pipeline orchestrator.

  discovery → fetch + fingerprint → quality gate → signals → maturity/contact →
  persist observation → sheet row

The old `db_lock` that serialised every database write through one synchronous
call is gone; writes now run concurrently under their own bounded semaphore,
which was the pipeline's throughput ceiling.
"""

import argparse
import asyncio
import logging
import os
import sys
from typing import Any, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from supabase import Client, create_client

import db_writer
import discovery
from config import (
    COUNTRIES,
    MIN_SIGNAL_SCORE,
    NICHES,
    RETRY_ATTEMPTS,
    SCRAPER_CONCURRENCY,
    SIGNAL_SLUGS,
    STORE_FRESHNESS_DAYS,
)
from contacts import extract_contact
from fetcher import STATUS_OK, build_client, fetch_store
from maturity import TIER_ESTABLISHED, TIER_GROWING, TIER_SEED, score_maturity
from outreach import build_angle, signal_score
from signals import classify_country, classify_niche, generate_signals, to_usd

load_dotenv(os.path.join(os.path.dirname(__file__), "../backend/.env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
# httpx logs full request URLs at INFO. Discovery passes the SerpAPI key as a
# query parameter, so leaving this at INFO writes the key in plaintext to the
# console and to any captured log.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("scraper")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

VALID_NICHES = ["all"] + NICHES
VALID_COUNTRIES = ["all"] + COUNTRIES

TIER_ORDER = {TIER_SEED: 0, TIER_GROWING: 1, TIER_ESTABLISHED: 2}


class RunMetrics:
    def __init__(self) -> None:
        self.discovered = 0
        self.scraped = 0
        self.skipped = 0
        self.failed = 0
        self.gated = 0
        self.signals = 0
        self.rows = 0
        self.db_failures = 0
        self.with_contact = 0

    def line(self) -> str:
        return (
            f"[PROGRESS] discovered={self.discovered} scraped={self.scraped} "
            f"skipped={self.skipped} failed={self.failed} quality_gated={self.gated} "
            f"signals={self.signals} sheet_rows={self.rows} contacts={self.with_contact} "
            f"db_failures={self.db_failures}"
        )


def _load_urls_file(path: str) -> list[str]:
    """Accept a plain URL list or a discovery-spike JSON.

    Lets a run reuse domains already discovered, so producing a sheet costs no
    SerpAPI credits.
    """
    import json as _json
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read().strip()
    if text.startswith("{") or text.startswith("["):
        data = _json.loads(text)
        reports = data.get("reports", []) if isinstance(data, dict) else data
        out: list[str] = []
        for report in reports:
            out.extend(report.get("domains", []))
        seen: set[str] = set()
        return [u for u in out if not (u in seen or seen.add(u))]
    return [line.strip() for line in text.splitlines() if line.strip()]


def _resolve_targets(
    niche: Optional[str],
    country: Optional[str],
    limit: int,
    supabase: Optional[Client],
    refresh_stale: bool,
    freshness_threshold: int,
    url_arg: Optional[str],
    allow_unmetered: bool,
    urls_file: Optional[str],
) -> list[tuple[str, Optional[str], Optional[str]]]:
    """Return [(url, niche, country)] to process."""
    if urls_file:
        logger.info("=== PHASE 1: URLs from %s ===", urls_file)
        return [(u, niche, country) for u in _load_urls_file(urls_file)[:limit]]

    if url_arg:
        logger.info("=== PHASE 1: single URL — %s ===", url_arg)
        return [(url_arg, niche, country)]

    if refresh_stale:
        logger.info("=== PHASE 1: refresh stale (>%s days) ===", freshness_threshold)
        if supabase is None:
            raise RuntimeError("--refresh-stale requires database credentials")
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=freshness_threshold)).isoformat()
        try:
            res = (supabase.table("stores")
                   .select("url,niche,country")
                   .or_(f"last_checked_at.is.null,last_checked_at.lt.{cutoff}")
                   # A LIST, not a pre-formatted string: supabase-py iterates
                   # a string character by character, so "(dead,not_shopify)"
                   # became NOT IN ('(','d','e','a','d',...) — true for every
                   # row. The filter silently excluded nothing.
                   # dead and not_shopify are terminal verdicts; re-crawling
                   # them burns the refresh budget on domains that cannot
                   # become rows. `unreachable` is deliberately not excluded —
                   # that failure may have been ours, and retrying costs no
                   # SerpAPI credit.
                   .not_.in_("status", ["dead", "not_shopify"])
                   # Only markets we actually sell. US discovery has no
                   # structural market filter — `matches_market` returns True
                   # for any .com — so European and Canadian stores enter the
                   # catalog and can never become rows. Without this they were
                   # re-crawled every 7 days forever.
                   .in_("country", ["USA", "Australia"])
                   .order("last_checked_at", desc=False)
                   .limit(limit).execute())
        except Exception as exc:
            raise RuntimeError(f"Could not load stale stores: {exc}") from exc
        rows = res.data or []
        logger.info("Found %s stale stores (capped at --limit)", len(rows))
        return [(r["url"], r.get("niche"), r.get("country")) for r in rows]

    logger.info("=== PHASE 1: discovery — niche=%s country=%s limit=%s ===", niche, country, limit)
    known = db_writer.load_known_domains(supabase) if supabase else set()
    countries = COUNTRIES if country in (None, "all") else [country]

    targets: list[tuple[str, Optional[str], Optional[str]]] = []
    per_country = max(1, limit // len(countries))
    for market in countries:
        urls = discovery.discover_stores(
            niche=None if niche == "all" else niche,
            country=market,
            limit=per_country,
            supabase=supabase,
            known_domains=known,
            allow_unmetered=allow_unmetered,
        )
        # Niche and country are confirmed from the fingerprint after fetching,
        # so they are passed as hints only.
        targets.extend((url, None if niche == "all" else niche, market) for url in urls)
    return targets


async def run_scraper(
    *,
    niche: Optional[str],
    country: Optional[str],
    wanted_signals: list[str],
    limit: int,
    dry_run: bool,
    concurrency: int,
    refresh_stale: bool,
    freshness_threshold: int,
    url_arg: Optional[str],
    retry_attempts: int,
    min_maturity: Optional[str],
    require_contact: bool,
    min_signals: int = 1,
    min_score: int = MIN_SIGNAL_SCORE,
    allow_unmetered: bool,
    urls_file: Optional[str] = None,
    export_csv: Optional[str] = None,
) -> None:
    # The client is created even for a dry run. --dry-run means "write nothing",
    # NOT "cost nothing": discovery still buys SERP pages. Without a client the
    # quota gate was skipped entirely and `load_known_domains` returned empty,
    # so a dry run spent UNMETERED credits re-discovering domains already held.
    # Every write path below is guarded by `dry_run` independently.
    supabase: Optional[Client] = None
    if SUPABASE_URL and SUPABASE_KEY:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    elif not dry_run:
        raise RuntimeError("Supabase env vars missing (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)")

    if not dry_run and supabase is not None:
        # DB calls run in to_thread workers, and one client's httpx pool cannot
        # be shared across threads safely. Give each worker its own.
        db_writer.configure_client_factory(
            lambda: create_client(SUPABASE_URL, SUPABASE_KEY))

    targets = _resolve_targets(
        niche, country, limit, supabase, refresh_stale,
        freshness_threshold, url_arg, allow_unmetered, urls_file,
    )
    if not targets:
        logger.info("No target stores. Exiting.")
        return

    seen: set[str] = set()
    deduped: list[tuple[str, Optional[str], Optional[str]]] = []
    for url, n, c in targets:
        host = urlparse(url if "//" in url else f"https://{url}").netloc.lower()
        if host and host not in seen:
            seen.add(host)
            deduped.append((url, n, c))
    targets = deduped

    metrics = RunMetrics()
    metrics.discovered = len(targets)
    logger.info("=== PHASE 2: fetch + score (%s unique targets) ===", len(targets))

    sheet_id: Optional[str] = None
    if supabase is not None and not dry_run:
        sheet_id = db_writer.get_or_create_sheet(supabase, niche, country, wanted_signals)
        logger.info("Sheet: %s", sheet_id)

    sheet_rows: list[dict] = []
    fetch_sem = asyncio.Semaphore(concurrency)
    db_sem = asyncio.Semaphore(max(2, concurrency))

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        browser_context = await browser.new_context()
        client = build_client()

        async def process(url: str, niche_hint: Optional[str], country_hint: Optional[str]) -> None:
            async with fetch_sem:
                record = await fetch_store(
                    url, client, browser_context=browser_context, retry_attempts=retry_attempts,
                )

            status = record.get("status")
            if status != STATUS_OK:
                if status == "not_shopify":
                    metrics.skipped += 1
                else:
                    metrics.failed += 1
                logger.info("%s → %s (%s)", url, status,
                            (record.get("fetch_quality") or {}).get("reason"))
                # Record the outcome even though there is nothing to score. The
                # credit that found this domain is already spent; without a row
                # here, discovery has no memory of it and buys it again.
                if supabase is not None and not dry_run:
                    async with db_sem:
                        await asyncio.to_thread(
                            db_writer.record_unusable_domain, supabase, url, status)
                return

            fingerprint = record["fingerprint"]
            quality = record["fetch_quality"]
            signals, attributes = generate_signals(fingerprint, quality)

            if not quality.get("scoreable"):
                metrics.gated += 1
                logger.info("%s → quality gated (%s)", url, quality.get("reason"))
                # A live store we could not score this time — still worth a row,
                # for the same reason as an unreachable one: the credit that
                # found it is spent, and --refresh-stale can retry it for free.
                if supabase is not None and not dry_run:
                    async with db_sem:
                        await asyncio.to_thread(
                            db_writer.record_unusable_domain, supabase, url, status)
                return

            metrics.scraped += 1
            metrics.signals += len(signals)

            resolved_niche = niche_hint or classify_niche(fingerprint)
            resolved_country = classify_country(fingerprint) or country_hint

            avg_price_usd = to_usd(record.get("avg_price"), attributes.get("currency"))
            record["avg_price_usd"] = avg_price_usd

            maturity = score_maturity(attributes, record.get("product_count"), avg_price_usd)
            contact = extract_contact(fingerprint, record.get("subpages"))
            if contact.get("email"):
                metrics.with_contact += 1

            # ── Sheet eligibility: maturity → gap → contact, in that order ──
            # A store with no signals is never a row, whatever else it clears —
            # including one whose signals were suppressed as unreliable.
            # A single gap signal is not a qualification. Measured on 127 rows:
            # 96% of scraped stores carried at least one gap, and
            # no_loyalty_program alone fired on 88% — that is a census with a
            # nicer label, exactly the failure StoreInspect documented. Real
            # qualification is a combination: several gaps, maturity, contact.
            score = signal_score(signals)
            include = len(signals) >= min_signals and score >= min_score
            if include and min_maturity and \
                    TIER_ORDER.get(maturity["tier"], 0) < TIER_ORDER.get(min_maturity, 0):
                include = False
            if include and wanted_signals and not any(s in signals for s in wanted_signals):
                include = False
            if include and require_contact and not contact.get("email"):
                include = False

            if include and export_csv:
                from datetime import datetime as _dt, timezone as _tz
                from export import build_row
                sheet_rows.append(build_row(
                    record=record, signals=signals, attributes=attributes,
                    maturity=maturity, contact=contact,
                    niche=resolved_niche, country=resolved_country,
                    checked_at=_dt.now(_tz.utc).isoformat(timespec="seconds"),
                ))

            if dry_run:
                logger.info("[DRY-RUN] %s", url)
                logger.info("   niche=%s country=%s maturity=%s(%s) currency=%s",
                            resolved_niche, resolved_country, maturity["tier"],
                            maturity["score"], attributes.get("currency"))
                logger.info("   vendors: email=%s reviews=%s chat=%s loyalty=%s pixel=%s",
                            attributes["email_vendors"], attributes["review_vendors"],
                            attributes["chat_vendors"], attributes["loyalty_vendors"],
                            attributes["ad_pixel_present"])
                logger.info("   signals: %s", sorted(signals))
                for slug, payload in signals.items():
                    logger.info("     - %s (conf %.2f) %s", slug, payload["confidence"],
                                payload["evidence"])
                logger.info("   contact: %s (%s)", contact.get("email"), contact.get("source"))
                logger.info("   angle: %s", build_angle(signals, attributes, record))
                logger.info("   sheet eligible: %s (score=%s)", include, score)
                if include:
                    metrics.rows += 1
            elif supabase is not None:
                async with db_sem:
                    created, failures = await asyncio.to_thread(
                        db_writer.write_store,
                        supabase,
                        record=record,
                        signals=signals,
                        attributes=attributes,
                        maturity=maturity,
                        contact=contact,
                        niche=resolved_niche,
                        country=resolved_country,
                        sheet_id=sheet_id,
                        include_in_sheet=include,
                    )
                metrics.rows += created
                metrics.db_failures += failures
                if created:
                    logger.info("✓ %s → sheet row (%s signals)", url, len(signals))

            logger.info(metrics.line())

        try:
            await asyncio.gather(*(process(u, n, c) for u, n, c in targets))
        finally:
            await client.aclose()
            await browser_context.close()
            await browser.close()

    if supabase is not None and sheet_id and not dry_run:
        await asyncio.to_thread(db_writer.refresh_sheet_count, supabase, sheet_id)

    if export_csv:
        from export import summarize, write_sheet_csv
        write_sheet_csv(sheet_rows, export_csv)
        logger.info("=== SHEET: %s ===", export_csv)
        for line in summarize(sheet_rows, metrics.scraped):
            logger.info("%s", line)

    logger.info("=== FINAL ===")
    logger.info(metrics.line())


def main() -> None:
    parser = argparse.ArgumentParser(description="Shopify leads scraper")
    parser.add_argument("--niche")
    parser.add_argument("--country", help=f"one of {VALID_COUNTRIES}")
    parser.add_argument("--signals", default="all",
                        help=f"comma-separated slugs, or 'all'. Available: {','.join(SIGNAL_SLUGS)}")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=SCRAPER_CONCURRENCY)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--refresh-stale", action="store_true")
    parser.add_argument("--freshness-threshold", type=int, default=STORE_FRESHNESS_DAYS)
    parser.add_argument("--url")
    parser.add_argument("--retry-attempts", type=int, default=RETRY_ATTEMPTS)
    parser.add_argument("--min-maturity", choices=[TIER_SEED, TIER_GROWING, TIER_ESTABLISHED],
                        help="Filter sheet rows by maturity before gap signals (recommended)")
    parser.add_argument("--min-signals", type=int, default=1,
                        help="Minimum gap signals for a sheet row. 1 qualifies ~96%% of "
                             "stores, which is a census; 3 is a realistic filter.")
    parser.add_argument("--min-score", type=int, default=MIN_SIGNAL_SCORE,
                        help=f"Minimum weighted signal score (default {MIN_SIGNAL_SCORE}). "
                             "Weights scarce, high-value gaps above near-universal ones.")
    parser.add_argument("--require-contact", action="store_true",
                        help="Only sheet stores with a usable generic contact address")
    parser.add_argument("--urls-file",
                        help="Reuse already-discovered domains (spike JSON or URL list) — no credits")
    parser.add_argument("--export-csv", help="Write the sheet to this CSV path")
    parser.add_argument("--allow-unmetered-quota", action="store_true",
                        help="Bypass the SerpAPI quota guard. Use deliberately.")
    args = parser.parse_args()

    if not args.refresh_stale and not args.url and not args.urls_file and not args.country:
        parser.error("--country is required unless using --refresh-stale or --url")

    wanted = [] if args.signals == "all" else [s.strip() for s in args.signals.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in SIGNAL_SLUGS]
    if unknown:
        parser.error(f"unknown signal(s): {', '.join(unknown)}. Available: {', '.join(SIGNAL_SLUGS)}")

    def normalize(value: Optional[str], valid: list[str]) -> Optional[str]:
        if not value:
            return None
        lookup = {v.lower(): v for v in valid}
        return lookup.get(value.strip().lower(), value.strip().title())

    try:
        asyncio.run(run_scraper(
            niche=normalize(args.niche, VALID_NICHES),
            country=normalize(args.country, VALID_COUNTRIES),
            wanted_signals=wanted,
            limit=args.limit,
            dry_run=args.dry_run,
            concurrency=args.concurrency,
            refresh_stale=args.refresh_stale,
            freshness_threshold=args.freshness_threshold,
            url_arg=args.url,
            retry_attempts=args.retry_attempts,
            min_maturity=args.min_maturity,
            require_contact=args.require_contact,
            min_signals=args.min_signals,
            min_score=args.min_score,
            allow_unmetered=args.allow_unmetered_quota,
            urls_file=args.urls_file,
            export_csv=args.export_csv,
        ))
    except RuntimeError as exc:
        logger.error(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
