"""Signal generation — 8 base signals, computed confidence, evidence per signal.

Base signals only. Compound/derived signals (retention_opportunity,
agency_goldmine, …) are composed in the frontend from the signal array, so new
bundles can be invented, renamed or reweighted without re-scraping. That removed
the dependency registry, the mutable `DYNAMIC_DEPENDENCIES` global that was
written from concurrent coroutines, and every AND-gated derived rule.

Confidence is computed from `fetch quality × match strength`, not looked up.
A static table gave every store the same number, which told a buyer nothing.
"""

import logging
import re
from typing import Any, Optional

from config import (
    COUNTRY_TLDS,
    CURRENCY_TO_USD,
    DETECTOR_VERSION,
    MIN_BLOG_ARTICLES,
)
from detectors import build_attributes, sms_suppressed_by

logger = logging.getLogger(__name__)

# How certain absence is, before fetch quality is applied. Chat is highest among
# the vendor signals because a chat widget must load a visible third-party
# script to function at all; SEO is highest overall because it is a
# deterministic parse of the document we already hold.
BASE_CERTAINTY: dict[str, float] = {
    "no_email_marketing": 0.90,
    "no_sms_marketing": 0.88,
    "no_ad_pixel": 0.92,
    "no_reviews": 0.92,
    "no_loyalty_program": 0.92,
    "no_live_chat": 0.95,
    "no_blog_content": 0.95,
    "weak_seo_meta": 0.97,
}

MAX_CONFIDENCE = 0.98


def quality_factor(fetch_quality: dict[str, Any], attributes: dict[str, Any]) -> float:
    """Scale confidence by how well we could actually see the storefront."""
    if not fetch_quality.get("scoreable"):
        return 0.0

    factor = 0.80
    if (fetch_quality.get("html_bytes") or 0) >= 60_000:
        factor += 0.06
    if (fetch_quality.get("script_count") or 0) >= 10:
        factor += 0.06
    # If we positively identified at least one third-party vendor, we have
    # proven we can see vendor scripts on this page — which makes the absence
    # of others meaningfully more credible.
    if (attributes.get("app_count") or 0) >= 1:
        factor += 0.08
    return min(1.0, factor)


def _confidence(slug: str, factor: float) -> float:
    return round(min(MAX_CONFIDENCE, BASE_CERTAINTY.get(slug, 0.85) * factor), 3)


def generate_signals(
    fingerprint: dict[str, Any],
    fetch_quality: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (signals, attributes).

    `signals` maps slug → {confidence, evidence}. Empty when the fetch failed
    quality gating: `provider is None` must mean "absent", never "we could not
    see". A 403 challenge page is still HTML, and scoring one produced a store
    that looked like it had no apps installed at all.
    """
    attributes = build_attributes(fingerprint)

    if not fetch_quality.get("scoreable"):
        logger.info(
            "Fetch below quality floor (%s) — no signals emitted for %s",
            fetch_quality.get("reason"),
            fingerprint.get("base_url"),
        )
        return {}, attributes

    factor = quality_factor(fetch_quality, attributes)
    signals: dict[str, Any] = {}

    # When vendor scripts are proxied through the store's own subdomain
    # (CNAME cloaking, common on larger brands), the vendor host is hidden and
    # absence stops being evidence. Suppress every vendor-absence signal rather
    # than emit a confident false positive — precision is the gate, and a wrong
    # claim is visible to the buyer because we ship the evidence with it.
    # Structural signals below are unaffected: they never depended on hosts.
    proxied = attributes.get("first_party_proxy_suspected")
    if proxied:
        logger.info(
            "First-party proxy suspected for %s (%s third-party hosts on %s bytes) — "
            "vendor signals suppressed",
            fingerprint.get("base_url"),
            len(attributes.get("third_party_hosts") or []),
            fetch_quality.get("html_bytes"),
        )

    def fire(slug: str, evidence: dict[str, Any]) -> None:
        signals[slug] = {
            "confidence": _confidence(slug, factor),
            "evidence": {**evidence, "detector_version": DETECTOR_VERSION},
        }

    def fire_vendor(slug: str, evidence: dict[str, Any]) -> None:
        """Vendor-absence signals, suppressed when detection cannot see vendors."""
        if not proxied:
            fire(slug, evidence)

    # ── 1. Email marketing ──────────────────────────────────────────────────
    if not attributes["email_vendors"]:
        fire_vendor("no_email_marketing", {
            "checked_vendors": "klaviyo, omnisend, mailchimp, drip, sendlane, activecampaign, privy",
            "script_hosts_seen": (fingerprint.get("script_hosts") or [])[:15],
            "caveat": "Shopify Email is native and loads no third-party script; it cannot be detected from the storefront.",
        })

    # ── 2. SMS marketing ────────────────────────────────────────────────────
    suppressor = sms_suppressed_by(attributes)
    if not attributes["sms_vendors"]:
        if suppressor:
            logger.debug("no_sms_marketing suppressed by dual-product vendor %s", suppressor)
        else:
            fire_vendor("no_sms_marketing", {
                "checked_vendors": "attentive, postscript, yotpo sms, recart",
                "script_hosts_seen": (fingerprint.get("script_hosts") or [])[:15],
            })

    # ── 3. Advertising pixel ────────────────────────────────────────────────
    if not attributes["ad_pixel_present"]:
        fire_vendor("no_ad_pixel", {
            **attributes["ad_pixel_evidence"],
            "note": "Checked both direct pixel hosts and Shopify's sandboxed web-pixels-manager config.",
        })

    # ── 4. Reviews ──────────────────────────────────────────────────────────
    if not attributes["review_vendors"]:
        fire_vendor("no_reviews", {
            "checked_vendors": "judge.me, loox, yotpo, okendo, stamped, reviews.io, opinew",
            "widget_classes_seen": (fingerprint.get("class_tokens") or [])[:15],
        })

    # ── 5. Loyalty ──────────────────────────────────────────────────────────
    if not attributes["loyalty_vendors"]:
        fire_vendor("no_loyalty_program", {
            "checked_vendors": "smile.io, loyaltylion, growave, rivo, yotpo loyalty, bon",
            "script_hosts_seen": (fingerprint.get("script_hosts") or [])[:15],
        })

    # ── 6. Live chat ────────────────────────────────────────────────────────
    if not attributes["chat_vendors"]:
        fire_vendor("no_live_chat", {
            "checked_vendors": "tidio, gorgias, intercom, zendesk, tawk, crisp, re:amaze, shopify inbox",
            "script_hosts_seen": (fingerprint.get("script_hosts") or [])[:15],
        })

    # ── 7. Blog content ─────────────────────────────────────────────────────
    # Positive assertion from sitemap.xml, not "the URL returned 200" — Shopify
    # serves a status-200 soft-404 body, which made the old check near-useless.
    article_count = attributes.get("blog_article_count")
    if article_count is not None and article_count < MIN_BLOG_ARTICLES:
        fire("no_blog_content", {
            "blog_article_count": article_count,
            "threshold": MIN_BLOG_ARTICLES,
            "source": "sitemap.xml → sitemap_blogs_*.xml",
        })

    # ── 8. SEO metadata ─────────────────────────────────────────────────────
    seo_faults: list[str] = []
    if not attributes["has_meta_description"]:
        seo_faults.append(
            "missing_meta_description" if attributes["meta_description_length"] == 0
            else f"meta_description_too_short({attributes['meta_description_length']})"
        )
    if not attributes["has_og_title"]:
        seo_faults.append("missing_og_title")
    if attributes["h1_count"] != 1:
        seo_faults.append(f"h1_count={attributes['h1_count']}")
    title_len = attributes["title_length"]
    if title_len < 15 or title_len > 65:
        seo_faults.append(f"title_length={title_len}")

    if len(seo_faults) >= 2:
        fire("weak_seo_meta", {"faults": seo_faults})

    return signals, attributes


# ── Classification ───────────────────────────────────────────────────────────
# Both classifiers read the fingerprint rather than raw HTML so they stay
# replayable during an offline re-score.

NICHE_KEYWORDS: dict[str, list[str]] = {
    "Beauty":      ["skincare", "serum", "moisturizer", "foundation", "lipstick",
                    "concealer", "spf", "toner", "cleanser", "blush", "mascara"],
    "Fashion":     ["dress", "jeans", "hoodie", "sneakers", "blouse", "outfit",
                    "apparel", "clothing", "shirt", "jacket", "skirt", "tshirt"],
    "Fitness":     ["protein", "pre-workout", "gym", "resistance band", "supplement",
                    "whey", "creatine", "workout", "fitness", "dumbbell", "yoga"],
    "Jewelry":     ["necklace", "ring", "bracelet", "earring", "pendant",
                    "gemstone", "diamond", "charm"],
    "Pets":        ["dog", "cat", "puppy", "kitten", "pet food", "leash",
                    "paw", "collar", "treats", "aquarium"],
    "Home":        ["candle", "pillow", "blanket", "wall art", "decor",
                    "furniture", "lamp", "rug", "curtain", "bedding"],
    "Electronics": ["charger", "cable", "bluetooth", "wireless", "earbuds",
                    "phone case", "adapter", "battery", "usb", "speaker"],
    "Sports":      ["jersey", "cleats", "racket", "helmet",
                    "yoga mat", "running", "cycling", "swimming", "athletic"],
}


# Keywords are matched on word boundaries, not as raw substrings.
#
# `combined.count("ring")` fired inside spring, watering, offering, catering,
# measuring and engineering; `charm` inside charming; `cat` inside catering.
# A product feed reading "Spring Collection" scored as Jewelry. Same bug class
# as `oke-` matching inside `smoke-`, already fixed in fingerprint.py.
#
# The optional `(?:e?s)?` keeps plurals working — "rings" and "dresses" still
# match "ring" and "dress" — without letting a prefix match run on.
_NICHE_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    niche: [
        re.compile(rf"(?<![a-z0-9]){re.escape(kw)}(?:e?s)?(?![a-z0-9])")
        for kw in keywords
    ]
    for niche, keywords in NICHE_KEYWORDS.items()
}


def classify_niche(fingerprint: dict[str, Any]) -> Optional[str]:
    """Infer niche from sampled product titles plus title/meta text."""
    parts = list(fingerprint.get("product_titles") or [])
    parts.append(fingerprint.get("title") or "")
    meta = fingerprint.get("meta") or {}
    parts.append(meta.get("description") or "")
    combined = " ".join(parts).lower()
    if not combined.strip():
        return None

    scores = {
        niche: sum(len(pattern.findall(combined)) for pattern in patterns)
        for niche, patterns in _NICHE_PATTERNS.items()
    }
    if not any(scores.values()):
        return None
    return max(scores, key=lambda k: scores[k])


def classify_country(fingerprint: dict[str, Any]) -> Optional[str]:
    """Infer market from currency and domain TLD.

    Currency is the strongest available signal — far better than the old
    approach of counting "$" in the HTML, which matched every price on the page.
    """
    currency = (fingerprint.get("currency") or "").upper()
    base_url = (fingerprint.get("base_url") or "").lower()

    if currency == "AUD":
        return "Australia"
    if any(base_url.endswith(tld) or f"{tld}/" in base_url for tld in COUNTRY_TLDS["Australia"]):
        return "Australia"
    if currency == "USD":
        return "USA"
    return None


def to_usd(amount: Optional[float], currency: Optional[str]) -> Optional[float]:
    """Normalise a price before applying any threshold.

    Prices come from /products.json in the store's own currency. Comparing them
    raw put a 500 INR store and a 500 USD store on the same scale.
    """
    if amount is None:
        return None
    rate = CURRENCY_TO_USD.get((currency or "USD").upper())
    if rate is None:
        return None
    return round(float(amount) * rate, 2)
