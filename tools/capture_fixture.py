"""Capture a real storefront as a labelled test fixture.

    python tools/capture_fixture.py https://examplestore.com \
        --label no_email_marketing=true --label no_reviews=false

Fixtures store the *fingerprint*, not the HTML, so they stay small and re-score
exactly the way production does. Labels are ground truth and must be verified by
hand — open the store, check the page source for the vendor, then record it.

Target 40-60 fixtures spread across maturity tiers before trusting the precision
numbers from `pytest tests/test_precision.py`.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fetcher import build_client, fetch_store  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures")


def parse_label(raw: str) -> tuple[str, bool]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"--label expects slug=true|false, got {raw!r}")
    slug, value = raw.split("=", 1)
    normalized = value.strip().lower()
    if normalized not in ("true", "false"):
        raise argparse.ArgumentTypeError(f"label value must be true or false, got {value!r}")
    return slug.strip(), normalized == "true"


async def capture(url: str, labels: dict[str, bool], notes: str, use_browser: bool) -> str:
    browser_context = None
    playwright = browser = None
    if use_browser:
        from playwright.async_api import async_playwright
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        browser_context = await browser.new_context()

    try:
        async with build_client() as client:
            result = await fetch_store(url, client, browser_context=browser_context)
    finally:
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()

    if not result.get("fingerprint"):
        raise SystemExit(
            f"Could not capture {url}: status={result.get('status')} "
            f"reason={(result.get('fetch_quality') or {}).get('reason')}"
        )

    payload = {
        "url": result["url"],
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "status": result["status"],
        "fingerprint": result["fingerprint"],
        "fetch_quality": result["fetch_quality"],
        "product_count": result.get("product_count"),
        "avg_price": result.get("avg_price"),
        "labels": labels,
        "notes": notes,
    }

    os.makedirs(FIXTURE_DIR, exist_ok=True)
    slug = result["url"].replace("https://", "").replace("http://", "").rstrip("/")
    slug = "".join(ch if ch.isalnum() else "_" for ch in slug)[:60]
    path = os.path.join(FIXTURE_DIR, f"{slug}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--label", action="append", default=[], type=parse_label,
                        help="Ground truth, e.g. --label no_reviews=true (repeatable)")
    parser.add_argument("--notes", default="")
    parser.add_argument("--no-browser", action="store_true",
                        help="Skip the Playwright fallback (faster, fails on challenged stores)")
    args = parser.parse_args()

    path = asyncio.run(capture(args.url, dict(args.label), args.notes, not args.no_browser))
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
