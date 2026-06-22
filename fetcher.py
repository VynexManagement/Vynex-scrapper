import asyncio
import json
import logging
import re
from typing import Any, Optional
from config import TITLE_SEPARATORS

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _normalize_url(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"https://{url}"


def _extract_store_name(html: str, fallback_url: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return fallback_url
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    if not title:
        return fallback_url
    for sep in TITLE_SEPARATORS:
        if sep in title:
            return title.split(sep)[0].strip()
    return title


def _extract_product_stats(json_products: Optional[dict[str, Any]]) -> tuple[Optional[int], Optional[float]]:
    if not json_products:
        return None, None

    products = json_products.get("products", [])
    product_count = len(products)

    prices: list[float] = []
    for product in products:
        for variant in product.get("variants", []):
            try:
                prices.append(float(variant["price"]))
            except (KeyError, TypeError, ValueError):
                continue

    avg_price = round(sum(prices) / len(prices), 2) if prices else None
    return product_count, avg_price


async def _fetch_all_products(client: httpx.AsyncClient, base_url: str, headers: dict) -> Optional[dict]:
    """
    Paginates through /products.json to collect all products.
    Shopify max is 250 per page. Stops at page 5 (1250 products) to prevent abuse.
    """
    all_products = []
    page = 1
    MAX_PAGES = 5

    while page <= MAX_PAGES:
        products_url = f"{base_url.rstrip('/')}/products.json?limit=250&page={page}"
        try:
            response = await client.get(products_url, headers=headers)
            if response.status_code != 200:
                break
            data = response.json()
            products = data.get("products", [])
            if not products:
                break
            all_products.extend(products)
            if len(products) < 250:
                break  # last page
            page += 1
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as e:
            logger.warning("Products page %s fetch failed for %s: %s", page, base_url, e)
            break

    return {"products": all_products} if all_products else None


async def _fetch_with_httpx(url: str) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    headers = {"User-Agent": USER_AGENT}
    timeout = httpx.Timeout(15.0, connect=5.0)

    html: Optional[str] = None
    products_json: Optional[dict[str, Any]] = None

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        try:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                html = response.text
        except httpx.HTTPError as e:
            logger.warning("HTML fetch failed for %s: %s (status=%s)", url, e,
                           getattr(e.response, 'status_code', 'N/A') if hasattr(e, 'response') else 'N/A')

        products_json = await _fetch_all_products(client, url, headers)

    return html, products_json


async def _fetch_with_playwright(url: str, context) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    page = await context.new_page()

    async def route_intercept(route):
        resource_type = route.request.resource_type
        if resource_type in ["image", "stylesheet", "font", "media", "other"]:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", route_intercept)

    try:
        await page.goto(url, timeout=12_000, wait_until="domcontentloaded")
        html = await page.content()

        products_json: Optional[dict[str, Any]] = None
        try:
            products_url = url.rstrip("/") + "/products.json?limit=250"
            response = await page.request.get(products_url, timeout=10_000)
            if response.ok:
                products_json = await response.json()
        except Exception:
            products_json = None

        return html, products_json
    finally:
        await page.close()


DEAD_STORE_MARKERS = [
    "this store is currently unavailable",
    "id=\"shopify-section-password",
    "opening soon",
    "coming soon",
    "enter using password",
    "your store is temporarily unavailable",
]

def is_dead_store(html: str) -> bool:
    """
    Returns True if the store is password-protected, coming soon,
    or otherwise unavailable. These should never become leads.
    """
    html_lower = html.lower()
    return any(marker in html_lower for marker in DEAD_STORE_MARKERS)


def is_shopify_store(html: str, json_products: Optional[dict] = None) -> bool:
    """
    Returns True if the HTML content or products data matches Shopify indicators.
    """
    if json_products and "products" in json_products:
        return True

    html_lower = html.lower()
    indicators = [
        "cdn.shopify.com",
        "shopify.theme",
        "shopifyanalytics",
        "shopify-payment-button",
        "content=\"shopify\"",
        "window.shopify",
        "/collections/all",
    ]
    return any(indicator in html_lower for indicator in indicators)


async def _fetch_subpage(client: httpx.AsyncClient, base_url: str, path: str, headers: dict) -> Optional[str]:
    url = f"{base_url.rstrip('/')}{path}"
    try:
        response = await client.get(url, headers=headers, timeout=10.0)
        if response.status_code == 200:
            final_path = response.url.path.rstrip('/')
            if final_path in ["", "/"]:
                return None
            return response.text
    except Exception:
        pass
    return None


async def _fetch_subpages(client: httpx.AsyncClient, base_url: str, headers: dict) -> dict[str, Optional[str]]:
    paths = {
        "about_us": "/pages/about-us",
        "about": "/pages/about",
        "contact": "/pages/contact",
        "blog_news": "/blogs/news",
        "blog": "/blog",
        "shipping": "/policies/shipping-policy",
        "refund": "/policies/refund-policy",
        "faq_page": "/pages/faq",
        "faq": "/faq",
    }
    subpage_contents = {}

    async def fetch_one(key: str, path: str):
        subpage_contents[key] = await _fetch_subpage(client, base_url, path, headers)

    tasks = [fetch_one(key, path) for key, path in paths.items()]
    await asyncio.gather(*tasks)
    return subpage_contents


def detect_email_provider(html: str) -> Optional[str]:
    html_lower = html.lower()
    providers = {
        "klaviyo": "klaviyo",
        "mailchimp": "mailchimp",
        "omnisend": "omnisend",
        "drip": "drip",
        "sendlane": "sendlane",
        "activecampaign": "activecampaign",
        "postscript": "postscript",
        "attentive": "attentive",
        "privy": "privy",
        "recart": "recart",
        "smsbump": "smsbump",
    }
    for key, name in providers.items():
        if key in html_lower:
            return name
    return None


def detect_review_provider(html: str) -> Optional[str]:
    html_lower = html.lower()
    providers = {
        "judge.me": "judge.me",
        "judge-me": "judge-me",
        "loox": "loox",
        "yotpo": "yotpo",
        "stamped": "stamped",
        "okendo": "okendo",
        "reviews.io": "reviews.io",
        "ali reviews": "ali reviews",
        "rivyo": "rivyo",
        "ryviu": "ryviu",
        "opinew": "opinew",
    }
    for key, name in providers.items():
        if key in html_lower:
            return name
    return None


def detect_loyalty_provider(html: str) -> Optional[str]:
    html_lower = html.lower()
    providers = {
        "smile.io": "smile.io",
        "loyaltylion": "loyaltylion",
        "growave": "growave",
        "yotpo loyalty": "yotpo loyalty",
        "stamped loyalty": "stamped loyalty",
        "rivo": "rivo",
        "bon-loyalty": "bon-loyalty",
    }
    for key, name in providers.items():
        if key in html_lower:
            return name
    return None


def detect_chat_provider(html: str) -> Optional[str]:
    html_lower = html.lower()
    providers = {
        "tidio": "tidio",
        "gorgias": "gorgias",
        "intercom": "intercom",
        "zendesk": "zendesk",
        "freshchat": "freshchat",
        "livechat": "livechat",
        "tawk.to": "tawk.to",
        "drift": "drift",
    }
    for key, name in providers.items():
        if key in html_lower:
            return name
    return None


def detect_upsell_provider(html: str) -> Optional[str]:
    html_lower = html.lower()
    providers = {
        "reconvert": "reconvert",
        "zipify": "zipify",
        "bold upsell": "bold upsell",
        "frequently bought": "frequently bought",
        "carthook": "carthook",
        "aftersell": "aftersell",
    }
    for key, name in providers.items():
        if key in html_lower:
            return name
    return None


def detect_trust_provider(html: str) -> Optional[str]:
    html_lower = html.lower()
    providers = {
        "mcafee": "mcafee",
        "norton": "norton",
        "trustpilot": "trustpilot",
        "trusted-site": "trusted-site",
        "buysafe": "buysafe",
        "shopify-security-badge": "shopify-security-badge",
        "ssl-certificate": "ssl-certificate",
        "comodo": "comodo",
        "verisign": "verisign",
    }
    for key, name in providers.items():
        if key in html_lower:
            return name
    return None


def count_social_links(html: str) -> int:
    html_lower = html.lower()
    social_tokens = ["instagram.com/", "facebook.com/", "tiktok.com/", "x.com/", "twitter.com/"]
    count = 0
    for token in social_tokens:
        if token in html_lower:
            count += 1
    return count


def extract_store_attributes(html: str, subpage_contents: dict[str, Optional[str]]) -> dict[str, Any]:
    combined_html = html
    for content in subpage_contents.values():
        if content:
            combined_html += "\n" + content

    email_provider = detect_email_provider(combined_html)
    review_provider = detect_review_provider(combined_html)
    loyalty_provider = detect_loyalty_provider(combined_html)
    chat_provider = detect_chat_provider(combined_html)
    upsell_provider = detect_upsell_provider(combined_html)
    trust_provider = detect_trust_provider(combined_html)
    social_links_count = count_social_links(combined_html)

    has_blog = bool(subpage_contents.get("blog_news") or subpage_contents.get("blog"))
    has_about_page = bool(subpage_contents.get("about_us") or subpage_contents.get("about"))
    has_faq = bool(
        subpage_contents.get("faq_page") or
        subpage_contents.get("faq") or
        "faq" in html.lower() or
        "frequently asked questions" in html.lower()
    )
    has_shipping_policy = bool(subpage_contents.get("shipping"))
    has_refund_policy = bool(subpage_contents.get("refund"))

    has_contact_page = bool(subpage_contents.get("contact"))
    has_contact_email = "@" in html
    has_contact = has_contact_page or has_contact_email or "contact" in html.lower()

    # Calculate opportunity score
    opportunity_score = 0
    if email_provider is None:
        opportunity_score += 2
    if review_provider is None:
        opportunity_score += 2
    if loyalty_provider is None:
        opportunity_score += 2
    if chat_provider is None:
        opportunity_score += 1
    if upsell_provider is None:
        opportunity_score += 2
    if not has_blog:
        opportunity_score += 1

    return {
        "email_provider": email_provider,
        "review_provider": review_provider,
        "loyalty_provider": loyalty_provider,
        "chat_provider": chat_provider,
        "upsell_provider": upsell_provider,
        "trust_provider": trust_provider,
        "social_links_count": social_links_count,
        "has_blog": has_blog,
        "has_about_page": has_about_page,
        "has_faq": has_faq,
        "has_shipping_policy": has_shipping_policy,
        "has_refund_policy": has_refund_policy,
        "has_contact": has_contact,
        "has_meta_desc": 'name="description"' in html.lower(),
        "has_og_title": 'property="og:title"' in html.lower(),
        "opportunity_score": opportunity_score,
    }


async def fetch_store_data(url: str, browser_context=None) -> dict[str, Any]:
    normalized_url = _normalize_url(url)

    html, json_products = await _fetch_with_httpx(normalized_url)

    if not html and browser_context is not None:
        logger.info("HTTP fetch failed for %s; using Playwright fallback", normalized_url)
        html, pw_products = await _fetch_with_playwright(normalized_url, browser_context)
        if pw_products and not json_products:
            json_products = pw_products

    if html and is_dead_store(html):
        logger.info("Dead/unavailable store detected: %s", normalized_url)
        return {
            "url": normalized_url,
            "html": "",         # empty HTML signals dead store to scraper.py
            "json_products": None,
            "product_count": None,
            "avg_price": None,
            "store_name": normalized_url,
            "is_dead": True,    # explicit flag
            "is_shopify": False,
        }

    if html and not is_shopify_store(html, json_products):
        logger.info("Store is not a valid Shopify store: %s", normalized_url)
        return {
            "url": normalized_url,
            "html": "",
            "is_shopify": False,
        }

    headers = {"User-Agent": USER_AGENT}
    subpages = {}
    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        if html:
            subpages = await _fetch_subpages(client, normalized_url, headers)

    product_count, avg_price = _extract_product_stats(json_products)
    
    attributes = extract_store_attributes(html or "", subpages) if html else {}

    return {
        "url": normalized_url,
        "html": html or "",
        "json_products": json_products,
        "product_count": product_count,
        "avg_price": avg_price,
        "store_name": _extract_store_name(html or "", normalized_url),
        "is_shopify": True,
        "attributes": attributes,
    }
