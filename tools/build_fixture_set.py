"""Build a labelled fixture set from real discovered stores.

Ground truth must not come from the detector under test, or the precision
measurement is circular. So each store gets two independent readings:

  loose   a high-recall substring scan of the raw HTML for vendor brand tokens.
          This is essentially the OLD detection method — it over-fires (that is
          the point), so when it finds nothing we can be confident nothing is
          there.
  strict  the current fingerprint + host-anchored detector.

Labelling rule per category:

  loose finds nothing            -> gap is TRUE   (confident, auto-labelled)
  strict finds a vendor          -> gap is FALSE  (confident, auto-labelled)
  loose hits but strict does not -> AMBIGUOUS, left unlabelled and flagged

The third bucket is the interesting one: it is where a real false positive of
the old method (privy/privacy, drip/"drip coffee") is indistinguishable from a
real miss of the new one. Those need a human to open the page — they are
written to review_queue.json rather than silently guessed.

    python tools/build_fixture_set.py --count 40
    python tools/build_fixture_set.py --count 5 --sample-seed 1   # smoke test
"""

import argparse
import asyncio
import glob
import json
import logging
import os
import random
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from config import MIN_BLOG_ARTICLES  # noqa: E402
from detectors import build_attributes  # noqa: E402
from fetcher import (  # noqa: E402
    USER_AGENT,
    assess_quality,
    build_client,
    count_blog_articles,
    is_shopify_store,
    normalize_url,
    product_stats,
    _fetch_products,
    _get,
)
from fingerprint import build_fingerprint  # noqa: E402
from vendors import CATEGORIES, VENDORS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("fixtures")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures")
SPIKE_GLOB = os.path.join(ROOT, "scratch", "spike_*.json")

CATEGORY_TO_SIGNAL = {
    "email": "no_email_marketing",
    "sms": "no_sms_marketing",
    "ad_pixel": "no_ad_pixel",
    "reviews": "no_reviews",
    "loyalty": "no_loyalty_program",
    "chat": "no_live_chat",
}

# Deliberately loose brand tokens — the high-recall baseline. Over-firing here
# is desirable: it is what lets "found nothing" be trusted.
LOOSE_TOKENS: dict[str, list[str]] = {
    "email": ["klaviyo", "omnisend", "mailchimp", "getdrip", "sendlane",
              "activecampaign", "privy", "mc4wp", "list-manage"],
    "sms": ["attentive", "attn.tv", "postscript", "smsbump", "recart"],
    "ad_pixel": ["facebook.net", "fbq(", "googletagmanager", "gtag(",
                 "analytics.tiktok", "ttq.", "pinimg.com/ct", "sc-static.net"],
    # NOTE: "oke-" (Okendo) is deliberately absent — it matches inside smoke-,
    # spoke-, broke-, choke- and flagged most stores as ambiguous. A loose token
    # may over-fire on vendors, but not on ordinary English.
    "reviews": ["judge.me", "judgeme", "jdgm", "loox", "yotpo", "okendo",
                "okereviews", "stamped.io", "reviews.io", "opinew", "spr-badge",
                "rivyo", "ryviu"],
    "loyalty": ["smile.io", "loyaltylion", "growave", "socialshopwave", "rivo.io",
                "swellrewards", "bonloyalty", "yotpo-loyalty"],
    "chat": ["tidio", "gorgias", "intercom", "zdassets", "zendesk", "tawk.to",
             "crisp.chat", "reamaze", "chat.shopify"],
}


def load_candidates() -> list[str]:
    domains: set[str] = set()
    for path in glob.glob(SPIKE_GLOB):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for report in data.get("reports", []):
            domains.update(report.get("domains", []))
    return sorted(domains)


def loose_scan(html: str) -> dict[str, list[str]]:
    lowered = html.lower()
    return {
        category: [token for token in tokens if token in lowered]
        for category, tokens in LOOSE_TOKENS.items()
    }


async def capture_one(client: httpx.AsyncClient, domain: str) -> dict | None:
    url = normalize_url(domain)
    response = await _get(client, url, attempts=2)
    if response is None or response.status_code != 200:
        logger.info("  skip %s (http %s)", domain,
                    response.status_code if response else "no response")
        return None

    html = response.text
    products = await _fetch_products(client, url)
    if not is_shopify_store(html, products):
        logger.info("  skip %s (not shopify)", domain)
        return None

    fingerprint = build_fingerprint(html, url)
    count, avg_price, titles = product_stats(products)
    fingerprint["product_titles"] = titles
    fingerprint["blog_article_count"] = await count_blog_articles(client, url)

    quality = assess_quality(html, fingerprint, "httpx")
    if not quality["scoreable"]:
        logger.info("  skip %s (quality: %s)", domain, quality["reason"])
        return None

    attributes = build_attributes(fingerprint)
    loose = loose_scan(html)

    labels: dict[str, bool] = {}
    ambiguous: dict[str, list[str]] = {}

    for category in CATEGORIES:
        signal = CATEGORY_TO_SIGNAL[category]
        loose_hits = loose.get(category, [])

        if category == "ad_pixel":
            strict_found = attributes["ad_pixel_present"]
        else:
            strict_found = bool(attributes[{
                "email": "email_vendors", "sms": "sms_vendors",
                "reviews": "review_vendors", "loyalty": "loyalty_vendors",
                "chat": "chat_vendors",
            }[category]])

        # We deliberately decline to claim `no_sms_marketing` when a vendor
        # selling both email and SMS is present, because the storefront cannot
        # say which product the merchant pays for. That is an abstention, not a
        # miss — labelling it True would score a design decision as an error.
        if category == "sms" and any(
            token in loose.get("email", []) for token in ("klaviyo", "omnisend", "sendlane")
        ):
            ambiguous[signal] = ["dual-product email vendor present — not claimable"]
            continue

        if strict_found:
            labels[signal] = False          # vendor present -> no gap
        elif not loose_hits:
            labels[signal] = True           # nothing anywhere -> real gap
        else:
            ambiguous[signal] = loose_hits  # needs a human

    # Blog is structural and independently verifiable from the sitemap.
    articles = fingerprint.get("blog_article_count")
    if articles is not None:
        labels["no_blog_content"] = articles < MIN_BLOG_ARTICLES

    return {
        "url": url,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "fingerprint": fingerprint,
        "fetch_quality": quality,
        "product_count": count,
        "avg_price": avg_price,
        "labels": labels,
        "ambiguous": ambiguous,
        "loose_scan": {k: v for k, v in loose.items() if v},
        "label_method": "independent loose-scan vs strict detector; ambiguous left unlabelled",
    }


async def main_async(count: int, seed: int, concurrency: int) -> None:
    candidates = load_candidates()
    if not candidates:
        logger.error("No spike results found in scratch/spike_*.json")
        return
    logger.info("%s candidate domains available", len(candidates))

    random.Random(seed).shuffle(candidates)
    os.makedirs(FIXTURE_DIR, exist_ok=True)

    captured: list[dict] = []
    review_queue: list[dict] = []
    semaphore = asyncio.Semaphore(concurrency)

    async with build_client() as client:
        async def work(domain: str) -> None:
            if len(captured) >= count:
                return
            async with semaphore:
                try:
                    result = await capture_one(client, domain)
                except Exception as exc:
                    logger.info("  skip %s (%s)", domain, exc)
                    return
            if not result or len(captured) >= count:
                return
            captured.append(result)
            slug = "".join(c if c.isalnum() else "_" for c in
                           result["url"].replace("https://", ""))[:60]
            with open(os.path.join(FIXTURE_DIR, f"{slug}.json"), "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=2, sort_keys=True)
            if result["ambiguous"]:
                review_queue.append({"url": result["url"], "ambiguous": result["ambiguous"]})
            logger.info("  ok   %s  labels=%s ambiguous=%s",
                        result["url"], len(result["labels"]), len(result["ambiguous"]))

        # Over-sample: many candidates will be dead, blocked or not Shopify.
        await asyncio.gather(*(work(d) for d in candidates[: count * 4]))

    os.makedirs(os.path.join(ROOT, "scratch"), exist_ok=True)
    with open(os.path.join(ROOT, "scratch", "review_queue.json"), "w", encoding="utf-8") as fh:
        json.dump(review_queue, fh, indent=2)

    total_labels = sum(len(f["labels"]) for f in captured)
    total_ambiguous = sum(len(f["ambiguous"]) for f in captured)
    logger.info("\n%s", "=" * 56)
    logger.info("Captured fixtures      : %s", len(captured))
    logger.info("Auto-labelled signals  : %s", total_labels)
    logger.info("Ambiguous (need human) : %s  -> scratch/review_queue.json", total_ambiguous)
    logger.info("Fixtures written to    : tests/fixtures/")
    logger.info("Now run: pytest tests/test_precision.py -q -s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--sample-seed", type=int, default=7)
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()
    asyncio.run(main_async(args.count, args.sample_seed, args.concurrency))


if __name__ == "__main__":
    main()
