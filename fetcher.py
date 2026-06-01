import asyncio
import json
import logging
import re
from typing import Any, Optional

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
    return title.split("|")[0].strip()


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
        except httpx.HTTPError:
            pass

        try:
            products_url = url.rstrip("/") + "/products.json?limit=250"
            response = await client.get(products_url, headers=headers)
            if response.status_code == 200:
                products_json = response.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            pass

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


async def fetch_store_data(url: str, browser_context=None) -> dict[str, Any]:
    normalized_url = _normalize_url(url)

    html, json_products = await _fetch_with_httpx(normalized_url)

    if not html and browser_context is not None:
        logger.info("HTTP fetch failed for %s; using Playwright fallback", normalized_url)
        html, pw_products = await _fetch_with_playwright(normalized_url, browser_context)
        if pw_products and not json_products:
            json_products = pw_products

    product_count, avg_price = _extract_product_stats(json_products)

    return {
        "url": normalized_url,
        "html": html or "",
        "json_products": json_products,
        "product_count": product_count,
        "avg_price": avg_price,
        "store_name": _extract_store_name(html or "", normalized_url),
    }
