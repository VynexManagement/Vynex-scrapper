import logging
import re
import time
import os
from typing import Optional
import httpx
from supabase import create_client, Client

logger = logging.getLogger(__name__)

SERPAPI_URL = "https://serpapi.com/search"
GOOGLE_DAILY_REQUEST_LIMIT = 100

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


def _get_serpapi_quota(supabase_client: Client) -> tuple[int, int]:
    try:
        res = supabase_client.table("signals").select("*").eq("slug", "serpapi_quota").execute()
        if res.data:
            row = res.data[0]
            return int(row.get("description") or 250), int(row.get("rule_definition") or 2)
        else:
            # Seed default values: 250 limit, 2 consumed
            insert_res = supabase_client.table("signals").insert({
                "name": "SerpApi Quota",
                "slug": "serpapi_quota",
                "description": "250",
                "rule_definition": "2",
                "active": True,
                "is_active": True,
                "type": "system_meta"
            }).execute()
            row = insert_res.data[0]
            return int(row.get("description") or 250), int(row.get("rule_definition") or 2)
    except Exception as e:
        logger.error(f"Failed to get/seed SerpApi quota in DB: {e}")
        return 250, 2


def _increment_serpapi_quota(supabase_client: Client, new_consumed: int):
    try:
        supabase_client.table("signals").update({
            "rule_definition": str(new_consumed)
        }).eq("slug", "serpapi_quota").execute()
    except Exception as e:
        logger.error(f"Failed to update SerpApi quota in DB: {e}")


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
    api_key = os.getenv("SERPAPI_API_KEY", "").strip() or os.getenv("GOOGLE_SEARCH_API_KEY", "").strip()
    if not api_key:
        logger.error("SerpApi API key missing (SERPAPI_API_KEY / GOOGLE_SEARCH_API_KEY)")
        return []

    # Check monthly quota in DB before searching
    supabase_client = _get_supabase_client()
    monthly_limit = 250
    monthly_consumed = 2
    if supabase_client:
        monthly_limit, monthly_consumed = _get_serpapi_quota(supabase_client)
        logger.info("Fetched SerpApi Quota from DB: %s/%s consumed", monthly_consumed, monthly_limit)
        if monthly_consumed >= monthly_limit:
            logger.warning(
                "SerpApi monthly quota exhausted (%s/%s). Discovery will not execute any search requests.",
                monthly_consumed,
                monthly_limit,
            )
            return []

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
                if requests_used >= GOOGLE_DAILY_REQUEST_LIMIT:
                    logger.warning(
                        "Daily request budget reached (%s). Stopping discovery early.",
                        GOOGLE_DAILY_REQUEST_LIMIT,
                    )
                    break

                # Double check monthly quota inside pagination loop
                if monthly_consumed >= monthly_limit:
                    logger.warning("Monthly SerpApi quota reached. Halting pagination.")
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
                    if c_lower == "usa":
                        params["gl"] = "us"
                    elif c_lower == "uk":
                        params["gl"] = "uk"
                    elif c_lower == "canada":
                        params["gl"] = "ca"
                    elif c_lower == "australia":
                        params["gl"] = "au"
                    elif c_lower == "india":
                        params["gl"] = "in"

                payload = _request_serpapi_page(client, params=params)
                requests_used += 1

                # Increment and sync database quota
                if supabase_client:
                    monthly_consumed += 1
                    _increment_serpapi_quota(supabase_client, monthly_consumed)
                    logger.info("Updated SerpApi monthly quota in DB: %s/%s consumed", monthly_consumed, monthly_limit)

                if payload is None:
                    logger.warning("Discovery stopping for query due to repeated HTTP failures: %s", query)
                    break

                organic_results = payload.get("organic_results") or []
                if not organic_results:
                    logger.info("No more organic results found for query: %s", query)
                    break

                text_blob = " ".join(
                    [
                        item.get("link", "")
                        + " "
                        + item.get("title", "")
                        + " "
                        + item.get("snippet", "")
                        for item in organic_results
                    ]
                )
                urls = _extract_myshopify_urls(text_blob)
                new_urls = [u for u in urls if u not in all_urls]
                new_urls_count = len(new_urls)
                logger.info("NEW URLs this run: %s", new_urls_count)

                all_urls.update(new_urls)
                logger.info("TOTAL URLs collected: %s", len(all_urls))

                start_index += 10
                page += 1
                logger.info("Discovery pagination page=%s query=%s", page, query)
                time.sleep(max(delay_seconds, 0.5))

    result = list(all_urls)[:limit]
    logger.info("Discovery complete: %s URLs found, requests_used=%s", len(result), requests_used)
    return result