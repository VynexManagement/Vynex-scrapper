"""Supabase persistence for the rebuilt pipeline.

Three deliberate departures from the old writer:

  * **Observations are append-only.** The old `insert_raw_data` skipped the
    write when the content hash matched — exactly backwards for change
    detection, which needs two observations separated by time and cannot be
    backfilled. One row per crawl, always.
  * **A lead is a store, once.** The old model wrote one `leads` row per
    matched signal, so a bad store with 15 signals became 15 leads and inflated
    `total_leads` — the number the product is priced on.
  * **No HTML is stored.** `store_raw_data.html` went through a cleaner that
    stripped `<script>` tags, i.e. the only thing every detector depends on, so
    stored data could not reproduce our own signals. Fingerprints can.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional, TypeVar

from supabase import Client

from config import DETECTOR_VERSION, SCRAPER_VERSION
from fingerprint import fingerprint_hash
from outreach import build_angle, summarize_evidence

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ── Thread-safety ────────────────────────────────────────────────────────────
# supabase-py is synchronous and wraps an httpx connection pool that is **not
# thread-safe**. The pipeline dispatches it through asyncio.to_thread, so a
# single shared client was being driven by several worker threads at once. That
# corrupts the pool: it surfaced as "Server disconnected" and, once, as
# "deque mutated during iteration" from inside httpx's own pool bookkeeping.
# Roughly 12% of observations were lost to it in the 398-domain migration run,
# silently — the writer logged failures but the run still exited 0.
#
# Each worker thread therefore gets its own client. asyncio.to_thread reuses a
# small pool of threads, so this creates a handful of clients per run, not one
# per store.
_local = threading.local()
_client_factory: Optional[Callable[[], Any]] = None


def configure_client_factory(factory: Callable[[], Any]) -> None:
    """Register how to mint a fresh client for a worker thread."""
    global _client_factory
    _client_factory = factory


def _client(supabase: Client) -> Client:
    """The calling thread's own client, or the shared one if unconfigured.

    Falling back to the passed client keeps tests and any caller that has not
    registered a factory working — single-threaded use was never the problem.
    """
    if _client_factory is None:
        return supabase
    own = getattr(_local, "client", None)
    if own is None:
        own = _local.client = _client_factory()
    return own


# A dropped connection can still happen legitimately (idle keep-alive reaped
# server-side). The socket is dead, not the request, so replaying is safe:
# upsert_store is keyed on domain, add_sheet_row is an upsert, and
# record_unusable_domain treats a duplicate-key as success. insert_observation
# is the one non-idempotent caller, and duplicating an observation is harmless
# where losing one is not — an append-only history with holes cannot be
# backfilled.
_TRANSIENT = (
    "server disconnected", "connection reset", "remote end closed",
    "connection aborted", "timed out", "temporarily unavailable",
)


def _retrying(what: str, call: Callable[[], T], attempts: int = 3) -> T:
    for attempt in range(attempts):
        try:
            return call()
        except Exception as exc:
            transient = any(marker in str(exc).lower() for marker in _TRANSIENT)
            if attempt == attempts - 1 or not transient:
                raise
            delay = 0.5 * (2 ** attempt)
            logger.warning("Transient DB error on %s (%s); retrying in %.1fs",
                           what, exc, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


# ── Reads ────────────────────────────────────────────────────────────────────

def load_known_domains(supabase: Client) -> set[str]:
    """Every domain we already hold, so discovery never pays for a repeat."""
    known: set[str] = set()
    page, size = 0, 1000
    while True:
        try:
            res = (_client(supabase).table("stores").select("domain")
                   .range(page * size, (page + 1) * size - 1).execute())
        except Exception as exc:
            logger.error("Could not load known domains: %s", exc)
            break
        rows = res.data or []
        known.update(r["domain"] for r in rows if r.get("domain"))
        if len(rows) < size:
            break
        page += 1
    logger.info("Loaded %s known domains for pre-spend dedup", len(known))
    return known


def load_active_signal_ids(supabase: Client) -> dict[str, str]:
    try:
        res = _client(supabase).table("signals").select("id,slug").eq("is_active", True).execute()
        return {row["slug"]: row["id"] for row in (res.data or [])}
    except Exception as exc:
        logger.error("Could not load signals: %s", exc)
        return {}


def latest_observation(supabase: Client, store_id: str) -> Optional[dict[str, Any]]:
    try:
        # `attributes` is load-bearing: write_changes diffs the previous
        # observation's vendor set against the current one. Omitting it here
        # made previous_attributes always None, so change detection silently
        # recorded nothing on every run.
        res = _retrying("latest_observation", lambda: (
            _client(supabase).table("store_observations")
            .select("fingerprint,signals,attributes,observed_at")
            .eq("store_id", store_id)
            .order("observed_at", desc=True).limit(1).execute()))
        return (res.data or [None])[0]
    except Exception as exc:
        logger.warning("Could not read last observation for %s: %s", store_id, exc)
        return None


# ── Writes ───────────────────────────────────────────────────────────────────

def upsert_store(
    supabase: Client,
    *,
    url: str,
    domain: str,
    name: str,
    country: Optional[str],
    niche: Optional[str],
    status: str,
    product_count: Optional[int],
    avg_price: Optional[float],
    avg_price_usd: Optional[float],
    currency: Optional[str],
    maturity: Optional[dict[str, Any]],
    contact: Optional[dict[str, Any]],
    shop_handle: Optional[str],
) -> Optional[str]:
    now = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "url": url,
        "domain": domain,
        "name": name or domain,
        "status": status,
        "product_count": product_count,
        "avg_price": avg_price,
        "avg_price_usd": avg_price_usd,
        "currency": currency,
        "shop_handle": shop_handle,
        "last_checked_at": now,
    }
    if maturity:
        payload["maturity_tier"] = maturity.get("tier")
        payload["maturity_score"] = maturity.get("score")
    if contact and contact.get("email"):
        payload["contact_email"] = contact["email"]
        payload["contact_source"] = contact.get("source")

    try:
        existing = _retrying("store lookup", lambda: (
            _client(supabase).table("stores").select("id,niche,country")
            .eq("domain", domain).execute()))
        if existing.data:
            store_id = existing.data[0]["id"]
            # Don't overwrite a resolved niche/country with an unknown one.
            if niche and not existing.data[0].get("niche"):
                payload["niche"] = niche
            if country and not existing.data[0].get("country"):
                payload["country"] = country
            _retrying("store update", lambda: (
                _client(supabase).table("stores").update(payload).eq("id", store_id).execute()))
            return store_id

        payload["niche"] = niche
        payload["country"] = country
        payload["first_seen_at"] = now
        res = _retrying("store insert", lambda: (
            _client(supabase).table("stores").insert(payload).execute()))
        return res.data[0]["id"]
    except Exception as exc:
        logger.error("Store upsert failed for %s: %s", domain, exc)
        return None


def record_unusable_domain(supabase: Client, url: str, fetch_status: Optional[str]) -> bool:
    """Remember a domain that produced no fingerprint.

    Discovery dedups against `stores` before spending a credit. A domain that
    came back dead, unreachable, or not-Shopify used to return before any write,
    so it stayed invisible and every later discovery run paid for it again — 116
    of 398 domains in the migration run. Storing the outcome makes the credit
    buy something permanent. `unreachable` is not terminal: --refresh-stale
    retries those for free, since the failure may have been ours.
    """
    from urllib.parse import urlparse

    domain = urlparse(url).netloc.lower()
    if not domain:
        return False
    now = datetime.now(timezone.utc).isoformat()
    payload = {"url": url, "domain": domain, "name": domain,
               "status": store_status(fetch_status), "last_checked_at": now}
    try:
        existing = _retrying("unusable lookup", lambda: (
            _client(supabase).table("stores").select("id").eq("domain", domain).execute()))
        if existing.data:
            _retrying("unusable update", lambda: (
                _client(supabase).table("stores").update(payload)
                .eq("id", existing.data[0]["id"]).execute()))
        else:
            # upsert, not insert: `stores.domain` is unique, and a retry after a
            # dropped connection can replay a write the server already applied.
            _retrying("unusable upsert", lambda: (
                _client(supabase).table("stores")
                .upsert({**payload, "first_seen_at": now}, on_conflict="domain").execute()))
        return True
    except Exception as exc:
        if "23505" in str(exc) or "duplicate key" in str(exc).lower():
            return True  # the row exists, which is the whole point
        logger.error("Could not record unusable domain %s: %s", domain, exc)
        return False


def insert_observation(
    supabase: Client,
    store_id: str,
    fingerprint: dict[str, Any],
    signals: dict[str, Any],
    attributes: dict[str, Any],
    fetch_quality: dict[str, Any],
) -> bool:
    """Append one observation. Never skipped — history is the product."""
    try:
        _retrying("insert_observation", lambda: _client(supabase).table("store_observations").insert({
            "store_id": store_id,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "fingerprint": fingerprint,
            "fingerprint_hash": fingerprint_hash(fingerprint),
            "signals": sorted(signals.keys()),
            "signal_detail": {k: v for k, v in signals.items()},
            "attributes": {k: v for k, v in attributes.items() if k != "detections"},
            "fetch_quality": fetch_quality,
            "scraper_version": SCRAPER_VERSION,
            "detector_version": DETECTOR_VERSION,
        }).execute())
        return True
    except Exception as exc:
        logger.error("Observation insert failed for %s: %s", store_id, exc)
        return False


# A fetch outcome is not a store's lifecycle state. `fetch_store` returns "ok",
# but `stores.status` means active | dead | not_shopify | unreachable, and every
# query that filters for active stores was matching nothing because each row
# said "ok".
FETCH_STATUS_TO_STORE_STATUS = {
    "ok": "active",
    "dead": "dead",
    "not_shopify": "not_shopify",
    "fetch_failed": "unreachable",
}


def store_status(fetch_status: Optional[str]) -> str:
    return FETCH_STATUS_TO_STORE_STATUS.get(fetch_status or "", "unreachable")


def _vendor_set(attributes: dict[str, Any]) -> set[str]:
    keys = ("email_vendors", "sms_vendors", "review_vendors", "loyalty_vendors", "chat_vendors")
    return {slug for key in keys for slug in (attributes.get(key) or [])}


def write_changes(
    supabase: Client,
    store_id: str,
    previous_attributes: Optional[dict[str, Any]],
    current_attributes: dict[str, Any],
) -> int:
    """Diff consecutive observations into install/uninstall events.

    An uninstall is a timed buying trigger, which is worth considerably more
    than a static gap — Store Leads gates exactly this behind their $250/mo tier.
    """
    if not previous_attributes:
        return 0

    before = _vendor_set(previous_attributes)
    after = _vendor_set(current_attributes)
    events = [{"change_type": "installed", "vendor": v} for v in sorted(after - before)]
    events += [{"change_type": "uninstalled", "vendor": v} for v in sorted(before - after)]
    if not events:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    try:
        _retrying("write_changes", lambda: _client(supabase).table("store_changes").insert([
            {**event, "store_id": store_id, "detected_at": now} for event in events
        ]).execute())
        logger.info("Recorded %s change event(s) for store %s", len(events), store_id)
        return len(events)
    except Exception as exc:
        logger.error("Change write failed for %s: %s", store_id, exc)
        return 0


# ── Sheets ───────────────────────────────────────────────────────────────────

def get_or_create_sheet(
    supabase: Client,
    niche: Optional[str],
    country: Optional[str],
    signal_slugs: list[str],
    price_usd: int = 49,
) -> Optional[str]:
    key = f"{(niche or 'all').lower()}-{(country or 'all').lower()}-{'+'.join(sorted(signal_slugs)) or 'all'}"
    try:
        res = _client(supabase).table("sheets").select("id").eq("query_hash", key).execute()
        if res.data:
            return res.data[0]["id"]
        res = _client(supabase).table("sheets").insert({
            "niche": niche,
            "country": country,
            "signal_slugs": sorted(signal_slugs),
            "query_hash": key,
            "total_rows": 0,
            "price_usd": price_usd,
            "description": f"{niche or 'All'} stores in {country or 'all markets'}",
            "last_built_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        return res.data[0]["id"]
    except Exception as exc:
        logger.error("Sheet upsert failed for %s: %s", key, exc)
        return None


def add_sheet_row(
    supabase: Client,
    sheet_id: str,
    store_id: str,
    signals: dict[str, Any],
    attributes: dict[str, Any],
    record: Optional[dict[str, Any]] = None,
) -> bool:
    try:
        _retrying("add_sheet_row", lambda: _client(supabase).table("sheet_rows").upsert({
            "sheet_id": sheet_id,
            "store_id": store_id,
            "signals": sorted(signals.keys()),
            "evidence": summarize_evidence(signals),
            "angle": build_angle(signals, attributes, record),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="sheet_id,store_id").execute())
        return True
    except Exception as exc:
        logger.error("Sheet row write failed (%s, %s): %s", sheet_id, store_id, exc)
        return False


def refresh_sheet_count(supabase: Client, sheet_id: str) -> None:
    try:
        res = (_client(supabase).table("sheet_rows").select("store_id", count="exact")
               .eq("sheet_id", sheet_id).execute())
        _client(supabase).table("sheets").update({
            "total_rows": res.count or 0,
            "last_built_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", sheet_id).execute()
    except Exception as exc:
        logger.error("Sheet count refresh failed for %s: %s", sheet_id, exc)


# ── Orchestrated write for one store ─────────────────────────────────────────

def write_store(
    supabase: Client,
    *,
    record: dict[str, Any],
    signals: dict[str, Any],
    attributes: dict[str, Any],
    maturity: Optional[dict[str, Any]],
    contact: Optional[dict[str, Any]],
    niche: Optional[str],
    country: Optional[str],
    sheet_id: Optional[str],
    include_in_sheet: bool,
) -> tuple[int, int]:
    """Persist one processed store. Returns (sheet_rows_created, db_failures)."""
    from urllib.parse import urlparse

    failures = 0
    fingerprint = record.get("fingerprint") or {}
    domain = urlparse(record["url"]).netloc.lower()

    store_id = upsert_store(
        supabase,
        url=record["url"],
        domain=domain,
        name=record.get("store_name") or domain,
        country=country,
        niche=niche,
        status=store_status(record.get("status")),
        product_count=record.get("product_count"),
        avg_price=record.get("avg_price"),
        avg_price_usd=record.get("avg_price_usd"),
        currency=attributes.get("currency"),
        maturity=maturity,
        contact=contact,
        shop_handle=attributes.get("shop_handle"),
    )
    if not store_id:
        logger.error("WRITE-FAIL upsert_store returned no id for %s", domain)
        return 0, 1

    if not fingerprint:
        logger.warning("WRITE-SKIP no fingerprint for %s (status=%s)",
                       domain, record.get("status"))
        return 0, failures

    previous = latest_observation(supabase, store_id)
    previous_attributes = (previous or {}).get("attributes") if previous else None

    if not insert_observation(supabase, store_id, fingerprint, signals, attributes,
                              record.get("fetch_quality") or {}):
        logger.error("WRITE-FAIL insert_observation for %s", domain)
        failures += 1

    write_changes(supabase, store_id, previous_attributes, attributes)

    created = 0
    if include_in_sheet and sheet_id and signals:
        if add_sheet_row(supabase, sheet_id, store_id, signals, attributes, record):
            created = 1
        else:
            logger.error("WRITE-FAIL add_sheet_row for %s", domain)
            failures += 1

    return created, failures
