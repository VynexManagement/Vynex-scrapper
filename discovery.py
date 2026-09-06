"""Store discovery via SerpAPI.

Three structural changes from the previous implementation:

  * **Custom domains, not myshopify subdomains.** `site:myshopify.com` finds
    only stores that never configured a custom domain — the youngest, smallest
    and least monetizable on the platform. Every store worth selling to an
    agency is on its own domain and was invisible to the old pipeline.
  * **Query permutations, not 3 static templates.** Query diversity is the real
    ceiling, not credits: Google stops producing new results for a single query
    after a few hundred, so buying more credits re-bought the same URLs.
  * **Cursors persist across runs.** The old code restarted every query at
    page 0 on every job and re-paid for domains already held.

Quota now fails **closed**. Failing open meant a database blip authorised
unmetered spend.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

import httpx

from config import (
    COUNTRY_GL_MAP,
    COUNTRY_TLDS,
    DISCOVERY_BREADTH_PAGES,
    DISCOVERY_MAX_PAGES_PER_QUERY,
    DISCOVERY_PAGE_SIZE,
    QUOTA_FAIL_OPEN,
    SERPAPI_MONTHLY_LIMIT,
)

logger = logging.getLogger(__name__)

SERPAPI_URL = "https://serpapi.com/search"

# Transport failures that happen *after* the request was dispatched, so SerpAPI
# ran the search and billed for it even though we never read the answer.
# Connect-phase errors (ConnectTimeout, ConnectError, PoolTimeout) and a
# half-sent request (WriteTimeout) never reached them and stay free.
_BILLED_TRANSPORT_ERRORS = (
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)

# A read timeout costs a full billed search and returns nothing, so waiting is
# strictly cheaper than retrying. Connect stays short — an unreachable SerpAPI
# should fail fast, since that failure really is free.
SERPAPI_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

EXCLUDED_DOMAINS = {
    "shopify.com", "myshopify.com", "google.com", "youtube.com",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "pinterest.com",
    "wikipedia.org", "github.com", "reddit.com", "amazon.com", "ebay.com",
    "etsy.com", "aliexpress.com", "linkedin.com", "tiktok.com", "medium.com",
    "trustpilot.com", "yelp.com", "quora.com",
}

# Storefront footprints that appear on custom-domain Shopify stores.
FOOTPRINTS = [
    '"powered by shopify"',
    '"powered by shopify" "add to cart"',
    'inurl:/collections/all "add to cart"',
]

# Product vocabulary per niche, used to fan queries out. Deliberately different
# words from the niche classifier's keywords so discovery and classification do
# not share a blind spot.
NICHE_TERMS: dict[str, list[str]] = {
    "Beauty":      ["skincare", "serum", "moisturiser", "lip balm", "cleanser", "face mask"],
    "Fashion":     ["dresses", "hoodies", "sneakers", "denim", "swimwear", "knitwear"],
    "Fitness":     ["protein powder", "gym wear", "resistance bands", "supplements", "yoga mat"],
    "Jewelry":     ["necklaces", "earrings", "bracelets", "engagement rings", "pendants"],
    "Pets":        ["dog treats", "cat toys", "pet beds", "dog collars", "pet grooming"],
    "Home":        ["scented candles", "cushions", "wall art", "bedding", "planters"],
    "Electronics": ["phone cases", "earbuds", "chargers", "smart home", "laptop stands"],
    "Sports":      ["cycling gear", "running shoes", "camping gear", "surfboards", "team jerseys"],
}

# Measured on the Phase 0 spike (2026-08-23), not guessed:
#
#   AU  site:*.com.au ................... 7.5 domains/credit, 100% .au purity
#   AU  AUD / "ships australia wide" .... 2.1 domains/credit
#
# The non-ccTLD forms mostly return .com results that `matches_market` then
# discards, so we were paying for results we throw away. AU is therefore scoped
# to ccTLD queries.
#
# KNOWN COVERAGE GAP: Australian merchants on a .com domain are invisible to
# this. Recovering them means relaxing `matches_market` for AU and letting the
# post-fetch currency check decide — which costs fetch time (free) instead of
# credits. Worth revisiting once AU volume matters.
MARKET_MODIFIERS: dict[str, list[str]] = {
    "USA": ['"free shipping" USA', '"ships from USA"', "USD"],
    "Australia": ["site:*.com.au", "site:*.net.au"],
}


# ── URL helpers ──────────────────────────────────────────────────────────────

def normalize_domain(url: str) -> Optional[str]:
    """Reduce any result URL to `https://<registrable-host>`."""
    try:
        parsed = urlparse(url if "//" in url else f"https://{url}")
        host = (parsed.netloc or parsed.path or "").lower().split("/")[0].split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        if not host or "." not in host:
            return None
        return f"https://{host}"
    except Exception as exc:
        logger.debug("Could not normalize %r: %s", url, exc)
        return None


# Shopify runs its own marketing site on many ccTLDs — shopify.com.au,
# shopify.co.uk — and none of them is a merchant storefront. Matching only
# "shopify.com" and its subdomains missed every one of those: AU discovery is
# scoped with `site:*.com.au`, which matches shopify.com.au exactly. It reached
# the fixture corpus as a labelled "store" carrying five trivially-true gap
# signals before anyone noticed.
PLATFORM_LABELS = {"shopify", "myshopify"}


def is_excluded(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return True
    if host in EXCLUDED_DOMAINS:
        return True
    # Any label being `shopify` catches shopify.com, shopify.com.au and
    # help.shopify.co.uk alike, while leaving shopifyexperts.com alone.
    if PLATFORM_LABELS & set(host.split(".")):
        return True
    # Marketplaces and blogs that dominate these SERPs.
    return any(host.endswith("." + blocked) for blocked in EXCLUDED_DOMAINS)


def is_myshopify(url: str) -> bool:
    return ".myshopify.com" in (urlparse(url).netloc or "").lower()


def matches_market(url: str, country: Optional[str]) -> bool:
    """TLD-based market filter.

    Google's `gl` parameter is a locale hint for the *searcher*, not a filter on
    store location — the old country targeting was effectively fiction. A ccTLD
    is weak but real evidence; currency from the fingerprint confirms it later.
    """
    if not country or country == "all" or country == "USA":
        return True  # .com is not evidence either way; confirm via currency
    host = (urlparse(url).netloc or "").lower()
    return any(host.endswith(tld) for tld in COUNTRY_TLDS.get(country, []))


# ── Quota ────────────────────────────────────────────────────────────────────

SERPAPI_ACCOUNT_URL = "https://serpapi.com/account"


def fetch_account_quota(api_key: str) -> Optional[dict[str, int]]:
    """SerpAPI's own counters — free to call, and the only authoritative source.

    Our ledger keys on the calendar month; SerpAPI resets on the account
    anniversary. Verified 30 Aug 2026: searches bought on 23 Aug already read as
    `this_month_usage 0`. Reading their counter sidesteps the mismatch instead of
    trying to guess the reset date.
    """
    try:
        with httpx.Client(timeout=20) as client:
            response = client.get(SERPAPI_ACCOUNT_URL, params={"api_key": api_key})
        if response.status_code != 200:
            # Never log the URL — it carries the API key as a query parameter.
            logger.warning("SerpAPI account check returned HTTP %s", response.status_code)
            return None
        data = response.json()
        return {
            "used": int(data.get("this_month_usage") or 0),
            "left": int(data.get("total_searches_left") or 0),
            "limit": int(data.get("searches_per_month") or SERPAPI_MONTHLY_LIMIT),
        }
    except Exception as exc:
        logger.warning("SerpAPI account check failed: %s", type(exc).__name__)
        return None


def sync_quota_from_account(supabase, api_key: str) -> Optional[dict[str, int]]:
    """Overwrite the local ledger with SerpAPI's truth. Returns their counters."""
    account = fetch_account_quota(api_key)
    if account is None or supabase is None:
        return account
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        supabase.table("api_quota").upsert({
            "service": "serpapi",
            "month": month_key,
            "used": account["used"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="service,month").execute()
    except Exception as exc:
        logger.warning("Could not sync quota ledger: %s", exc)
    return account


def record_extra_usage(supabase, extra: int) -> None:
    """Book attempts beyond the one the pre-flight gate already consumed.

    The result is deliberately ignored — these searches are already spent, so
    refusing them after the fact would only make the ledger wrong.
    """
    for _ in range(max(0, extra)):
        check_and_increment_quota(supabase)


def check_and_increment_quota(
    supabase,
    limit: int = SERPAPI_MONTHLY_LIMIT,
    fail_open: bool = QUOTA_FAIL_OPEN,
) -> bool:
    """Atomically consume one SerpAPI credit. Fails **closed** by default."""
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        res = supabase.rpc("increment_quota", {
            "p_service": "serpapi",
            "p_month": month_key,
            "p_limit": limit,
        }).execute()
        return bool(res.data)
    except Exception as exc:
        logger.error("Quota RPC failed: %s (fail_open=%s)", exc, fail_open)
        return fail_open


# ── Cursors ──────────────────────────────────────────────────────────────────

class CursorStore:
    """Remembers how deep each query has been paginated.

    Backed by Supabase when available, otherwise a local JSON file so dry runs
    and the discovery spike still avoid re-fetching the same pages.
    """

    LOCAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scratch", "cursors.json")

    def __init__(self, supabase=None):
        self.supabase = supabase
        self._local: dict[str, dict[str, Any]] = {}
        if supabase is None:
            self._load_local()

    def _load_local(self) -> None:
        try:
            with open(self.LOCAL_PATH, "r", encoding="utf-8") as handle:
                self._local = json.load(handle)
        except (OSError, ValueError):
            self._local = {}

    def _save_local(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.LOCAL_PATH), exist_ok=True)
            with open(self.LOCAL_PATH, "w", encoding="utf-8") as handle:
                json.dump(self._local, handle, indent=2)
        except OSError as exc:
            logger.warning("Could not persist cursors locally: %s", exc)

    def get(self, query: str) -> dict[str, Any]:
        key = query_key(query)
        if self.supabase is None:
            return self._local.get(key, {"next_start": 0, "exhausted": False})
        try:
            res = (self.supabase.table("discovery_cursors")
                   .select("next_start,exhausted").eq("query_hash", key).execute())
            if res.data:
                return res.data[0]
        except Exception as exc:
            logger.warning("Cursor read failed for %s: %s", key, exc)
        return {"next_start": 0, "exhausted": False}

    def get_many(self, queries: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Read every cursor at once, keyed by query string.

        One round trip instead of the per-query `get()` the scheduler would
        otherwise issue inside its loop. Ordering by depth needs all of them up
        front anyway, so this is strictly fewer calls than before, not more.

        A read failure degrades to "nothing explored yet" rather than halting:
        the cost is a run that schedules in arbitrary order, not a lost run.
        """
        queries = list(queries)
        by_key = {query_key(q): q for q in queries}
        unseen: dict[str, dict[str, Any]] = {
            q: {"next_start": 0, "exhausted": False} for q in queries
        }
        if self.supabase is None:
            for key, query in by_key.items():
                if key in self._local:
                    unseen[query] = self._local[key]
            return unseen
        try:
            res = (self.supabase.table("discovery_cursors")
                   .select("query_hash,next_start,exhausted")
                   .in_("query_hash", list(by_key)).execute())
            for row in res.data or []:
                query = by_key.get(row.get("query_hash"))
                if query is not None:
                    unseen[query] = row
        except Exception as exc:
            logger.warning("Bulk cursor read failed (%s) — scheduling unordered", exc)
        return unseen

    def set(self, query: str, next_start: int, exhausted: bool) -> None:
        key = query_key(query)
        record = {"next_start": next_start, "exhausted": exhausted}
        if self.supabase is None:
            self._local[key] = record
            self._save_local()
            return
        try:
            self.supabase.table("discovery_cursors").upsert({
                "query_hash": key,
                "query": query[:500],
                "next_start": next_start,
                "exhausted": exhausted,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="query_hash").execute()
        except Exception as exc:
            logger.warning("Cursor write failed for %s: %s", key, exc)


def query_key(query: str) -> str:
    from hashlib import sha256
    return sha256(query.encode("utf-8")).hexdigest()[:32]


# ── Query generation ─────────────────────────────────────────────────────────

def build_queries(niche: Optional[str], country: Optional[str]) -> list[str]:
    """Fan out into many distinct queries.

    8 niches x ~5 product terms x 3 footprints x market modifiers yields
    hundreds of distinct SERPs instead of the 3 the old code reused forever.
    """
    niches = list(NICHE_TERMS) if not niche or niche == "all" else [niche]
    modifiers = MARKET_MODIFIERS.get(country or "", [""]) or [""]

    queries: list[str] = []
    for niche_name in niches:
        for term in NICHE_TERMS.get(niche_name, [niche_name.lower()]):
            for footprint in FOOTPRINTS:
                for modifier in modifiers:
                    parts = [footprint, f'"{term}"']
                    if modifier:
                        parts.append(modifier)
                    # Exclude the subdomain population explicitly.
                    parts.append("-site:myshopify.com")
                    queries.append(" ".join(parts))

    # Stable order, de-duplicated. Stability matters because `query_key` hashes
    # the string: reordering is free, rewording orphans a cursor. Which query
    # runs first is the scheduler's problem — see `schedule_queries`.
    seen: set[str] = set()
    ordered: list[str] = []
    for query in queries:
        if query not in seen:
            seen.add(query)
            ordered.append(query)
    return ordered


def schedule_queries(
    queries: Iterable[str],
    cursors: dict[str, dict[str, Any]],
    seed: Optional[int] = None,
) -> list[str]:
    """Order queries least-explored first.

    `build_queries` emits a deterministic permutation — niches in dict order, so
    Beauty/skincare is always first — and `discover_stores` stops as soon as it
    has `limit` domains. Together those meant a `--limit 50` AU run never got
    past query six of 252: the same two Beauty queries were re-paginated deeper
    on every run while seven niches were never issued at all. That, not market
    saturation, is why 48% of results came back already-known on 6 Sep 2026.

    Sorting by `next_start` puts never-issued queries (0) first and pushes the
    deeply-drained ones last. Equal depths are shuffled so that ties — which on
    a fresh corpus means *everything* — rotate between runs instead of falling
    back to the dict order this exists to escape.
    """
    import random

    rng = random.Random(seed)
    ordered = list(queries)
    rng.shuffle(ordered)
    return sorted(
        ordered,
        key=lambda q: int((cursors.get(q) or {}).get("next_start") or 0),
    )


# ── SerpAPI ──────────────────────────────────────────────────────────────────

def _request_page(
    client: httpx.Client, params: dict[str, str], retries: int = 3
) -> tuple[Optional[dict], int]:
    """Fetch one SERP page. Returns (payload, searches_served).

    The second value is the whole point: every retry after the first is another
    real search. The ledger used to increment once per *page* while this
    function retried up to three times internally, so recorded consumption could
    be a third of actual.

    What counts as served turns on whether the request reached SerpAPI:

      * A **connect-phase** failure never arrived, so it is free. Same for a 429,
        which was refused rather than served.
      * A **read** timeout or a mid-response protocol error *did* arrive and was
        billed — we simply gave up reading the reply. Counting these as free was
        measured wrong on 6 Sep 2026: a run modelled 23 searches while SerpAPI's
        own counter charged 27, the gap being seven read timeouts.

    Exact billing semantics are not published, so this is still an
    approximation — `sync_quota_from_account` reconciles against their counter
    at the start and end of every run.
    """
    served = 0
    for attempt in range(1, retries + 1):
        try:
            response = client.get(SERPAPI_URL, params=params)
        except _BILLED_TRANSPORT_ERRORS as exc:
            # Reached SerpAPI and was served; we lost the response, not the search.
            served += 1
            logger.warning("SerpAPI request failed after dispatch (%s/%s): %s — counted as billed",
                           attempt, retries, exc)
            time.sleep(1.5 * attempt)
            continue
        except httpx.HTTPError as exc:
            logger.warning("SerpAPI request failed (%s/%s): %s", attempt, retries, exc)
            time.sleep(1.5 * attempt)
            continue

        if response.status_code == 429:
            logger.info("SerpAPI rate-limited (%s/%s)", attempt, retries)
            time.sleep(2.0 * attempt)
            continue

        served += 1
        try:
            response.raise_for_status()
            return response.json(), served
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("SerpAPI bad response (%s/%s): %s", attempt, retries, exc)
            time.sleep(1.5 * attempt)
    return None, served


def discover_stores(
    niche: Optional[str],
    country: Optional[str],
    limit: int = 50,
    supabase=None,
    known_domains: Optional[Iterable[str]] = None,
    delay_seconds: float = 1.0,
    allow_unmetered: bool = False,
    verify: bool = True,
) -> list[str]:
    """Return newly discovered custom-domain storefront URLs.

    `known_domains` is checked **before** a credit is spent, so re-discovering
    stores we already hold costs nothing.
    """
    api_key = os.getenv("SERPAPI_API_KEY", "").strip()
    if not api_key:
        logger.error("SERPAPI_API_KEY missing — cannot run discovery")
        return []

    # Reconcile the ledger against SerpAPI's own counter before spending, so a
    # run never starts believing it has headroom it does not have.
    account = sync_quota_from_account(supabase, api_key)
    if account is not None:
        logger.info("SerpAPI budget: %s used, %s left of %s",
                    account["used"], account["left"], account["limit"])
        if account["left"] <= 0 and not allow_unmetered:
            logger.error("No SerpAPI credits remaining — discovery aborted")
            return []

    known = {d.lower() for d in (known_domains or [])}
    cursors = CursorStore(supabase)
    queries = build_queries(niche, country)
    cursor_by_query = cursors.get_many(queries)
    queries = schedule_queries(queries, cursor_by_query)
    unexplored = sum(1 for c in cursor_by_query.values() if not int(c.get("next_start") or 0))

    # Breadth and depth were measured as independent multipliers, and breadth was
    # the one going unused: a small-limit run drained the head of the list while
    # hundreds of queries sat at page 0. While untouched queries remain, cap depth
    # so the run spends its credits across niches instead of down one.
    max_pages = DISCOVERY_BREADTH_PAGES if unexplored else DISCOVERY_MAX_PAGES_PER_QUERY
    logger.info(
        "Discovery: niche=%s country=%s limit=%s across %s distinct queries "
        "(%s never issued, max %s pages each)",
        niche, country, limit, len(queries), unexplored, max_pages,
    )

    found: dict[str, None] = {}
    credits_used = 0
    # Per-stage survivor counts. Without these a query that returns 100 results
    # of which 60 die at matches_market looks identical to a query that returns
    # nothing — and you retire the query instead of fixing the modifier.
    drops = {"excluded": 0, "myshopify": 0, "market": 0, "known": 0, "unverified": 0}

    with httpx.Client(follow_redirects=True, timeout=SERPAPI_TIMEOUT) as client:
        for query in queries:
            if len(found) >= limit:
                break

            cursor = cursor_by_query.get(query) or {"next_start": 0, "exhausted": False}
            if cursor.get("exhausted"):
                continue

            start = int(cursor.get("next_start") or 0)
            pages = 0

            while len(found) < limit and pages < max_pages:
                if supabase is not None and not allow_unmetered:
                    if not check_and_increment_quota(supabase):
                        logger.warning("SerpAPI quota exhausted — halting discovery")
                        return list(found)[:limit]

                params = {
                    "api_key": api_key,
                    "engine": "google",
                    "q": query,
                    "start": str(start),
                    "num": str(DISCOVERY_PAGE_SIZE),
                    "google_domain": "google.com",
                    "hl": "en",
                }
                gl = COUNTRY_GL_MAP.get((country or "").lower())
                if gl:
                    params["gl"] = gl

                payload, served = _request_page(client, params)
                credits_used += served
                # The gate above consumed one; book any further served searches.
                if supabase is not None and not allow_unmetered:
                    record_extra_usage(supabase, served - 1)
                if payload is None:
                    logger.warning("Abandoning query after repeated failures: %s", query)
                    break

                results = payload.get("organic_results") or []
                if not results:
                    cursors.set(query, start, exhausted=True)
                    break

                new_this_page = 0
                for item in results:
                    link = item.get("link")
                    if not link:
                        continue
                    domain = normalize_domain(link)
                    if not domain:
                        continue
                    if is_excluded(domain):
                        drops["excluded"] += 1
                        continue
                    if is_myshopify(domain):
                        drops["myshopify"] += 1
                        continue
                    if not matches_market(domain, country):
                        drops["market"] += 1
                        continue
                    host = urlparse(domain).netloc.lower()
                    if host in known or domain in found:
                        drops["known"] += 1
                        continue
                    # Confirm it is really Shopify before it costs a full fetch.
                    # Free: /products.json plus the x-shopid response headers.
                    # 27 of 433 stored stores are not_shopify because this check
                    # existed but was wired only into the discovery spike.
                    if verify and not verify_shopify_domain(client, domain):
                        drops["unverified"] += 1
                        continue
                    found[domain] = None
                    new_this_page += 1

                start += DISCOVERY_PAGE_SIZE
                pages += 1
                cursors.set(query, start, exhausted=False)
                logger.info(
                    "query=%.60s page=%s new=%s total=%s credits=%s",
                    query, pages, new_this_page, len(found), credits_used,
                )
                time.sleep(max(delay_seconds, 0.5))

    logger.info("Discovery complete: %s new domains, %s searches used", len(found), credits_used)
    logger.info("  dropped — excluded=%(excluded)s myshopify=%(myshopify)s "
                "market=%(market)s already-known=%(known)s not-shopify=%(unverified)s", drops)
    if credits_used:
        logger.info("  yield: %.1f new domains per search", len(found) / credits_used)

    after = sync_quota_from_account(supabase, api_key)
    if after is not None:
        logger.info("SerpAPI budget after run: %s used, %s left", after["used"], after["left"])

    return list(found)[:limit]


# ── Verification ─────────────────────────────────────────────────────────────

SHOPIFY_HEADER_HINTS = ("x-shopid", "x-shopify-stage", "x-sorting-hat-shopid")


def verify_shopify_domain(client: httpx.Client, domain: str) -> bool:
    """Confirm a candidate is a Shopify storefront without spending a credit.

    Cheap and definitive: Shopify serves /products.json and stamps identifying
    response headers.
    """
    try:
        response = client.get(f"{domain.rstrip('/')}/products.json?limit=1", timeout=10)
        if response.status_code == 200:
            try:
                if "products" in (response.json() or {}):
                    return True
            except ValueError:
                pass
        if any(h in response.headers for h in SHOPIFY_HEADER_HINTS):
            return True
    except httpx.HTTPError as exc:
        logger.debug("Verification failed for %s: %s", domain, exc)
    return False


MYSHOPIFY_RE = re.compile(r"https?://([a-z0-9\-]+)\.myshopify\.com", re.IGNORECASE)
