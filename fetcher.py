"""Storefront fetching with explicit quality gating.

Three behaviours here are deliberate reversals of the previous implementation:

  * Playwright engages on a **bad-but-present** response, not only an empty one.
    A 403 Cloudflare challenge is still HTML, so the old check passed it through
    as a successful fetch and scored a store that looked like it had zero apps
    installed — a perfect-looking false lead.
  * One shared client per run. The old code built a new AsyncClient per store,
    and another for subpages: no connection reuse, no shared cookie jar.
  * Below the quality floor we return a fetch failure and **no signals**.
    `provider is None` must mean "absent", never "we could not see".

Only 3 subpages are fetched, and only for contact extraction. Provider detection
reads the homepage alone — concatenating 9 subpages made detection sensitivity a
function of site size and inflated every vendor match.
"""

import asyncio
import json
import logging
import re
from typing import Any, Optional
from xml.etree import ElementTree

import httpx

from config import (
    HTTP_CONNECT_TIMEOUT_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    MIN_HTML_BYTES,
    MIN_SCRIPT_COUNT,
    RETRY_ATTEMPTS,
    SUBPAGE_TIMEOUT_SECONDS,
)
from fingerprint import build_fingerprint

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

STATUS_OK = "ok"
STATUS_DEAD = "dead"
STATUS_NOT_SHOPIFY = "not_shopify"
STATUS_FETCH_FAILED = "fetch_failed"

DEAD_STORE_MARKERS = [
    "this store is currently unavailable",
    'id="shopify-section-password',
    "opening soon",
    "coming soon",
    "enter using password",
    "your store is temporarily unavailable",
]

# A challenge page is HTML with a 200-ish shape but no storefront in it.
#
# Split into strong and weak markers deliberately. A loose `"captcha" in html`
# gates real stores: plenty of legitimate storefronts mention captcha on a
# contact form or load a Turnstile widget. Weak markers therefore only count on
# a page too small to be a storefront.
STRONG_CHALLENGE_MARKERS = [
    "cf-browser-verification",
    "cf-chl-",
    "checking your browser before accessing",
    "<title>just a moment",
    "please enable cookies and reload the page",
    "ray id:",
]

WEAK_CHALLENGE_MARKERS = [
    "captcha",
    "access denied",
    "attention required",
    "enable javascript and cookies to continue",
    "unusual traffic",
]

# A real storefront is comfortably larger than any bot wall.
CHALLENGE_SIZE_CEILING = 50_000

SHOPIFY_INDICATORS = [
    "cdn.shopify.com",
    "cdn/shop/",
    "shopify.theme",
    "shopifycloud",
    "shopifyanalytics",
    "shopify-payment-button",
    "window.shopify",
]

MAX_PRODUCT_PAGES = 5
PRODUCT_TITLE_SAMPLE = 40


def normalize_url(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"https://{url}"


def is_dead_store(html: str) -> bool:
    lowered = (html or "").lower()
    return any(marker in lowered for marker in DEAD_STORE_MARKERS)


def looks_like_challenge(html: str) -> bool:
    """True only when the page is a bot wall, not merely a page mentioning one."""
    lowered = (html or "").lower()
    if any(marker in lowered for marker in STRONG_CHALLENGE_MARKERS):
        return True
    if len(lowered) < CHALLENGE_SIZE_CEILING:
        return any(marker in lowered for marker in WEAK_CHALLENGE_MARKERS)
    return False


def is_shopify_store(html: str, products: Optional[dict] = None) -> bool:
    if products and products.get("products"):
        return True
    lowered = (html or "").lower()
    return any(indicator in lowered for indicator in SHOPIFY_INDICATORS)


def assess_quality(html: str, fingerprint: dict[str, Any], method: str) -> dict[str, Any]:
    """Decide whether this fetch may produce signals at all."""
    html_bytes = fingerprint.get("html_bytes") or 0
    script_count = fingerprint.get("script_count") or 0
    markers = fingerprint.get("shopify_markers") or []

    quality: dict[str, Any] = {
        "html_bytes": html_bytes,
        "script_count": script_count,
        "shopify_markers": markers,
        "method": method,
        "scoreable": False,
        "reason": None,
    }

    if looks_like_challenge(html):
        quality["reason"] = "bot_challenge"
        return quality
    if html_bytes < MIN_HTML_BYTES:
        quality["reason"] = f"html_too_small({html_bytes})"
        return quality
    if script_count < MIN_SCRIPT_COUNT:
        quality["reason"] = f"too_few_scripts({script_count})"
        return quality
    if not markers:
        quality["reason"] = "no_shopify_markers"
        return quality

    quality["scoreable"] = True
    return quality


# ── Low-level fetches ────────────────────────────────────────────────────────

async def _get(
    client: httpx.AsyncClient,
    url: str,
    attempts: int = RETRY_ATTEMPTS,
    timeout: Optional[float] = None,
) -> Optional[httpx.Response]:
    """GET with bounded retries. Errors are logged, never silently swallowed."""
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return await client.get(url, timeout=timeout or HTTP_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            logger.warning("GET failed (%s/%s) %s: %s", attempt, attempts, url, exc)
            if attempt < attempts:
                await asyncio.sleep(1.0 * attempt)
    return None


async def _fetch_products(client: httpx.AsyncClient, base_url: str) -> Optional[dict[str, Any]]:
    """Paginate /products.json. Capped to avoid hammering large catalogues."""
    collected: list[dict[str, Any]] = []
    for page in range(1, MAX_PRODUCT_PAGES + 1):
        url = f"{base_url.rstrip('/')}/products.json?limit=250&page={page}"
        response = await _get(client, url, attempts=1)
        if response is None or response.status_code != 200:
            break
        try:
            products = (response.json() or {}).get("products", [])
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("products.json unparseable for %s: %s", base_url, exc)
            break
        if not products:
            break
        collected.extend(products)
        if len(products) < 250:
            break
    return {"products": collected} if collected else None


def product_stats(products: Optional[dict[str, Any]]) -> tuple[Optional[int], Optional[float], list[str]]:
    """Return (count, average price, sampled titles)."""
    if not products:
        return None, None, []

    items = products.get("products", [])
    prices: list[float] = []
    titles: list[str] = []
    for product in items:
        title = product.get("title")
        if title and len(titles) < PRODUCT_TITLE_SAMPLE:
            titles.append(str(title)[:120])
        for variant in product.get("variants", []) or []:
            try:
                prices.append(float(variant["price"]))
            except (KeyError, TypeError, ValueError):
                continue

    avg = round(sum(prices) / len(prices), 2) if prices else None
    return len(items), avg, titles


async def count_blog_articles(client: httpx.AsyncClient, base_url: str) -> Optional[int]:
    """Count published blog articles via sitemap.xml.

    A positive assertion, unlike the old `/blogs/news` fetch which trusted any
    HTTP 200 — and Shopify returns 200 with a soft-404 body. Shopify only emits
    a sitemap_blogs_*.xml child when blogs with published articles exist.
    Returns None when the sitemap itself is unreachable (unknown, not zero).
    """
    response = await _get(client, f"{base_url.rstrip('/')}/sitemap.xml", attempts=1)
    if response is None or response.status_code != 200:
        return None

    try:
        root = ElementTree.fromstring(response.text)
    except ElementTree.ParseError:
        return None

    blog_sitemaps = [
        loc.text for loc in root.iter()
        if loc.tag.endswith("loc") and loc.text and "sitemap_blogs" in loc.text
    ]
    if not blog_sitemaps:
        return 0

    total = 0
    for sitemap_url in blog_sitemaps[:3]:
        child = await _get(client, sitemap_url, attempts=1)
        if child is None or child.status_code != 200:
            continue
        try:
            child_root = ElementTree.fromstring(child.text)
        except ElementTree.ParseError:
            continue
        total += sum(
            1 for loc in child_root.iter()
            if loc.tag.endswith("loc") and loc.text and "/blogs/" in loc.text
        )
    return total


async def _fetch_contact_subpages(client: httpx.AsyncClient, base_url: str) -> dict[str, Optional[str]]:
    """Only the pages that carry contact details. Not used for provider detection."""
    paths = {
        "contact": "/pages/contact",
        "shipping_policy": "/policies/shipping-policy",
        "refund_policy": "/policies/refund-policy",
    }
    out: dict[str, Optional[str]] = {}

    async def one(key: str, path: str) -> None:
        response = await _get(
            client, f"{base_url.rstrip('/')}{path}",
            attempts=1, timeout=SUBPAGE_TIMEOUT_SECONDS,
        )
        if response is not None and response.status_code == 200:
            # Reject the soft-404 that redirects back to the storefront root.
            final_path = response.url.path.rstrip("/")
            out[key] = None if final_path in ("", "/") else response.text
        else:
            out[key] = None

    await asyncio.gather(*(one(key, path) for key, path in paths.items()))
    return out


async def _fetch_with_playwright(url: str, context) -> Optional[str]:
    page = await context.new_page()

    async def block_assets(route):
        if route.request.resource_type in ("image", "stylesheet", "font", "media"):
            await route.abort()
        else:
            await route.continue_()

    try:
        await page.route("**/*", block_assets)
        await page.goto(url, timeout=20_000, wait_until="domcontentloaded")
        # Give sandboxed pixel/vendor scripts a moment to attach.
        await page.wait_for_timeout(1_500)
        return await page.content()
    except Exception as exc:
        logger.warning("Playwright fetch failed for %s: %s", url, exc)
        return None
    finally:
        await page.close()


# ── Orchestration ────────────────────────────────────────────────────────────

def build_client() -> httpx.AsyncClient:
    """One client per run — connection reuse and a shared cookie jar."""
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=HTTP_CONNECT_TIMEOUT_SECONDS),
        headers={"User-Agent": USER_AGENT},
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )


async def fetch_store(
    url: str,
    client: httpx.AsyncClient,
    browser_context=None,
    retry_attempts: int = RETRY_ATTEMPTS,
) -> dict[str, Any]:
    """Fetch one storefront and return a fingerprinted, quality-assessed record."""
    base_url = normalize_url(url)
    method = "httpx"

    response = await _get(client, base_url, attempts=retry_attempts)
    html = response.text if response is not None and response.status_code == 200 else None
    status_code = response.status_code if response is not None else None

    # Escalate to a real browser when the response is missing OR present but
    # unusable — a challenge page, a stub, or something with no Shopify markers.
    needs_browser = (
        html is None
        or looks_like_challenge(html)
        or len(html.encode("utf-8", errors="ignore")) < MIN_HTML_BYTES
        or not any(ind in html.lower() for ind in SHOPIFY_INDICATORS)
    )
    if needs_browser and browser_context is not None:
        logger.info("Escalating to Playwright for %s (http_status=%s)", base_url, status_code)
        rendered = await _fetch_with_playwright(base_url, browser_context)
        if rendered:
            html, method = rendered, "playwright"

    if not html:
        return {
            "url": base_url,
            "status": STATUS_FETCH_FAILED,
            "fetch_quality": {"scoreable": False, "reason": "no_html", "method": method,
                              "http_status": status_code},
            "fingerprint": None,
        }

    if is_dead_store(html):
        return {
            "url": base_url,
            "status": STATUS_DEAD,
            "fetch_quality": {"scoreable": False, "reason": "dead_store", "method": method},
            "fingerprint": None,
        }

    products = await _fetch_products(client, base_url)

    if not is_shopify_store(html, products):
        return {
            "url": base_url,
            "status": STATUS_NOT_SHOPIFY,
            "fetch_quality": {"scoreable": False, "reason": "not_shopify", "method": method},
            "fingerprint": None,
        }

    fingerprint = build_fingerprint(html, base_url)
    count, avg_price, titles = product_stats(products)
    fingerprint["product_titles"] = titles
    fingerprint["blog_article_count"] = await count_blog_articles(client, base_url)

    subpages = await _fetch_contact_subpages(client, base_url)
    quality = assess_quality(html, fingerprint, method)

    return {
        "url": base_url,
        "status": STATUS_OK,
        "fingerprint": fingerprint,
        "fetch_quality": quality,
        "subpages": subpages,
        "product_count": count,
        "avg_price": avg_price,
        "store_name": fingerprint.get("store_name"),
    }
