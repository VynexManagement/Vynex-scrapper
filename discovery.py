import logging
import re
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GOOGLE_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"
GOOGLE_DAILY_REQUEST_LIMIT = 100

SHOPIFY_URL_PATTERN = re.compile(
    r"https?://([a-z0-9\-]+)\.myshopify\.com", re.IGNORECASE
)


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


def _request_google_page(client: httpx.Client, params: dict[str, str], retries: int = 3) -> Optional[dict]:
    for attempt in range(1, retries + 1):
        try:
            response = client.get(GOOGLE_SEARCH_URL, params=params)
        except httpx.HTTPError as e:
            logger.warning("HTTP request failed on attempt %s: %s", attempt, e)
            time.sleep(1.5)
            continue

        if response.status_code == 429:
            logger.info("Google API rate-limited (attempt %s/%s)", attempt, retries)
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
    import os

    api_key = os.getenv("GOOGLE_SEARCH_API_KEY", "").strip()
    cx = os.getenv("GOOGLE_SEARCH_CX", "").strip()
    if not api_key or not cx:
        logger.error("Google Search credentials missing (GOOGLE_SEARCH_API_KEY / GOOGLE_SEARCH_CX)")
        return []

    all_urls: set[str] = set()
    queries = build_queries(niche)
    requests_used = 0
    logger.info(
        "Starting discovery: niche=%s country=%s limit=%s queries=%s",
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
            start_index = 1

            while len(all_urls) < limit:
                if requests_used >= GOOGLE_DAILY_REQUEST_LIMIT:
                    logger.warning(
                        "Google daily request budget reached (%s). Stopping discovery early.",
                        GOOGLE_DAILY_REQUEST_LIMIT,
                    )
                    break

                params = {
                    "key": api_key,
                    "cx": cx,
                    "q": query,
                    "start": str(start_index),
                    "num": "10",
                }

                payload = _request_google_page(client, params=params)
                requests_used += 1

                if payload is None:
                    logger.warning("Discovery stopping for query due to repeated HTTP failures: %s", query)
                    break

                text_blob = " ".join(
                    [
                        item.get("link", "")
                        + " "
                        + item.get("title", "")
                        + " "
                        + item.get("snippet", "")
                        for item in (payload.get("items") or [])
                    ]
                )
                urls = _extract_myshopify_urls(text_blob)
                new_urls = [u for u in urls if u not in all_urls]
                new_urls_count = len(new_urls)
                logger.info("NEW URLs this run: %s", new_urls_count)

                if new_urls_count == 0:
                    logger.info("No new URLs from current page; stopping pagination for this query")
                    break

                all_urls.update(new_urls)
                logger.info("TOTAL URLs collected: %s", len(all_urls))

                next_page = ((payload.get("queries") or {}).get("nextPage") or [])
                if not next_page:
                    logger.info("No further pagination token for query: %s", query)
                    break

                start_index = int(next_page[0].get("startIndex", start_index + 10))
                page += 1
                logger.info("Discovery pagination page=%s query=%s", page, query)
                time.sleep(max(delay_seconds, 0.5))

    result = list(all_urls)[:limit]

    logger.info("Discovery complete: %s URLs found, google_requests_used=%s", len(result), requests_used)
    return result