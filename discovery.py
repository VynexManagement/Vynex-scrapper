import logging
import re
import time
import os
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse
import httpx
from supabase import create_client, Client
from config import SERPAPI_MONTHLY_LIMIT, COUNTRY_GL_MAP

logger = logging.getLogger(__name__)

SERPAPI_URL = "https://serpapi.com/search"

EXCLUDED_DISCOVERY_DOMAINS = [
    "shopify.com",
    "myshopify.com",
    "google.com",
    "youtube.com",
    "facebook.com",
    "twitter.com",
    "instagram.com",
    "pinterest.com",
    "wikipedia.org",
    "github.com",
]

def normalize_domain(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        if not netloc and parsed.path:
            netloc = parsed.path.lower()
            
        # Strip www.
        if netloc.startswith("www."):
            netloc = netloc[4:]
            
        # Remove trailing path or queries
        netloc = netloc.split('/')[0]
        if not netloc:
            return None
        return f"https://{netloc}"
    except Exception:
        pass
    return None


def is_excluded_domain(url: str) -> bool:
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
            
        if netloc in EXCLUDED_DISCOVERY_DOMAINS:
            return True
            
        # Exclude Shopify portals (e.g. community.shopify.com)
        if netloc.endswith(".shopify.com") and netloc != "shopify.com":
            return True
            
        if netloc == "myshopify.com":
            return True
    except Exception:
        pass
    return False

SHOPIFY_URL_PATTERN = re.compile(
    r"https?://([a-z0-9\-]+)\.myshopify\.com", re.IGNORECASE
)


def _get_supabase_client() -> Optional[Client]:
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if url and key:
        try:
            return create_client(url, key)
        except Exception as e:
            logger.error(f"Failed to initialize Supabase client in discovery: {e}")
    return None


def check_and_increment_quota(supabase_client: Client, limit: int = SERPAPI_MONTHLY_LIMIT) -> bool:
    """
    Atomically increments the monthly SerpAPI request counter.
    Returns True if the request is allowed, False if quota is exhausted.
    Uses a Postgres RPC for atomicity — no read-then-write race condition.
    Fails open (returns True) if the DB call itself fails, so a DB blip
    does not silently kill an entire scrape job.
    """
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        res = supabase_client.rpc("increment_quota", {
            "p_service": "serpapi",
            "p_month": month_key,
            "p_limit": limit,
        }).execute()
        return bool(res.data)
    except Exception as e:
        logger.error(f"Quota RPC failed: {e}")
        return True  # fail open


def _extract_myshopify_urls(text: str) -> list[str]:
    found: set[str] = set()
    for match in SHOPIFY_URL_PATTERN.finditer(text):
        store_slug = match.group(1)
        found.add(f"https://{store_slug}.myshopify.com")
    return list(found)


def build_queries(niche: str) -> list[str]:
    queries = [
        'site:myshopify.com "Add to cart"',
        'site:myshopify.com "Buy now"',
        'site:myshopify.com "Powered by Shopify"',
    ]
    if niche and niche != "all":
        queries += [
            f'site:myshopify.com "{niche}" "Add to cart"',
            f'site:myshopify.com "{niche}" "Buy now"',
        ]
    return queries


def _request_serpapi_page(client: httpx.Client, params: dict[str, str], retries: int = 3) -> Optional[dict]:
    for attempt in range(1, retries + 1):
        try:
            response = client.get(SERPAPI_URL, params=params)
        except httpx.HTTPError as e:
            logger.warning("HTTP request failed on attempt %s: %s", attempt, e)
            time.sleep(1.5)
            continue

        if response.status_code == 429:
            logger.info("SerpApi rate-limited (attempt %s/%s)", attempt, retries)
            time.sleep(2)
            continue

        try:
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.warning("HTTP request status error on attempt %s: %s", attempt, e)
            time.sleep(1.5)

    return None


def discover_stores(
    niche: str,
    limit: int = 50,
    country: Optional[str] = None,
    delay_seconds: float = 1.0,
) -> list[str]:
    api_key = os.getenv("SERPAPI_API_KEY", "").strip()
    if not api_key:
        logger.error("SerpApi API key missing (SERPAPI_API_KEY)")
        return []

    supabase_client = _get_supabase_client()

    all_urls: set[str] = set()
    queries = build_queries(niche)
    requests_used = 0
    logger.info(
        "Starting discovery via SerpApi: niche=%s country=%s limit=%s queries=%s",
        niche,
        country,
        limit,
        len(queries),
    )

    with httpx.Client(follow_redirects=True, timeout=15) as client:
        for query in queries:
            if len(all_urls) >= limit:
                break

            logger.info("Discovery query: %s", query)
            page = 0
            start_index = 0

            while len(all_urls) < limit:
                if supabase_client and not check_and_increment_quota(supabase_client):
                    logger.warning("Monthly SerpAPI quota exhausted. Halting.")
                    break

                params = {
                    "api_key": api_key,
                    "engine": "google",
                    "q": query,
                    "start": str(start_index),
                    "num": "10",
                    "google_domain": "google.com",
                    "hl": "en",
                }

                if country and country != "all":
                    c_lower = country.lower()
                    if c_lower in COUNTRY_GL_MAP:
                        params["gl"] = COUNTRY_GL_MAP[c_lower]

                payload = _request_serpapi_page(client, params=params)
                requests_used += 1

                if payload is None:
                    logger.warning("Discovery stopping for query due to repeated HTTP failures: %s", query)
                    break

                organic_results = payload.get("organic_results") or []
                if not organic_results:
                    logger.info("No more organic results found for query: %s", query)
                    break

                new_urls_count = 0
                for item in organic_results:
                    link = item.get("link")
                    if link:
                        norm_url = normalize_domain(link)
                        if norm_url and not is_excluded_domain(norm_url) and norm_url not in all_urls:
                            all_urls.add(norm_url)
                            new_urls_count += 1
                logger.info("NEW URLs this run: %s", new_urls_count)
                logger.info("TOTAL URLs collected: %s", len(all_urls))

                start_index += 10
                page += 1
                logger.info("Discovery pagination page=%s query=%s", page, query)
                time.sleep(max(delay_seconds, 0.5))

    result = list(all_urls)[:limit]
    logger.info("Discovery complete: %s URLs found, requests_used=%s", len(result), requests_used)
    return result


def build_generic_queries() -> list[str]:
    """
    Generic queries for 'all' case — no niche or country filter.
    3 queries instead of 40. Massively reduces SerpAPI quota burn.
    """
    return [
        'site:myshopify.com "Add to cart"',
        'site:myshopify.com "Buy now"',
        'site:myshopify.com "Powered by Shopify"',
    ]


def discover_stores_generic(limit: int = 100, delay_seconds: float = 1.0) -> list[str]:
    """
    Generic Shopify store discovery with no niche/country filter.
    Used when niche='all' AND country='all'.
    Stores are later classified by classify_niche() and classify_country() in signals.py.
    """
    api_key = os.getenv("SERPAPI_API_KEY", "").strip()
    if not api_key:
        logger.error("SerpApi API key missing")
        return []

    supabase_client = _get_supabase_client()
    all_urls: set[str] = set()
    queries = build_generic_queries()

    with httpx.Client(follow_redirects=True, timeout=15) as client:
        for query in queries:
            if len(all_urls) >= limit:
                break
            start_index = 0
            while len(all_urls) < limit:
                if supabase_client and not check_and_increment_quota(supabase_client):
                    logger.warning("Quota exhausted during generic discovery.")
                    return list(all_urls)[:limit]

                params = {
                    "api_key": api_key,
                    "engine": "google",
                    "q": query,
                    "start": str(start_index),
                    "num": "10",
                    "google_domain": "google.com",
                    "hl": "en",
                }
                payload = _request_serpapi_page(client, params=params)
                if not payload:
                    break

                organic_results = payload.get("organic_results") or []
                if not organic_results:
                    break

                for item in organic_results:
                    link = item.get("link")
                    if link:
                        norm_url = normalize_domain(link)
                        if norm_url and not is_excluded_domain(norm_url):
                            all_urls.add(norm_url)
                start_index += 10
                time.sleep(max(delay_seconds, 0.5))

    return list(all_urls)[:limit]