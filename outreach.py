"""Turn evidence into a first-line outreach angle.

This is the leverage the evidence payload buys. Every competitor ships a boolean
column and leaves the buyer to write the email. The benchmark is under 1% reply
for generic outreach against 5-8% for personalised outreach to a qualified
store — so the opener, not the row count, is what the sheet sells.

An earlier version picked the top-priority gap and named one detected app, which
produced byte-identical sentences on 4 of 14 rows. Two stores with the same gap
but 8 products at $128 and 148 products at $48 are not the same prospect and
must not get the same sentence.

So each gap carries several openers, and the one chosen is the **most specific
that this store's facts can support**: a detected complementary app beats a
price point, which beats a catalogue size, which beats the generic fallback.
Selection is deterministic — the same store always produces the same line.
"""

from typing import Any, Callable, Optional

from config import SIGNAL_WEIGHTS
from vendors import VENDORS_BY_SLUG

# Ordered by what an agency will pay for, highest first.
ANGLE_PRIORITY = [
    "no_email_marketing",
    "no_sms_marketing",
    "no_reviews",
    "no_ad_pixel",
    "no_loyalty_program",
    "no_blog_content",
    "weak_seo_meta",
    "no_live_chat",
]

VENDOR_CATEGORY_LABEL = {
    "email": "email capture",
    "sms": "SMS capture",
    "reviews": "reviews",
    "loyalty": "a loyalty program",
    "chat": "live chat",
}


def signal_score(signals: dict[str, Any]) -> int:
    """Scarcity- and value-weighted qualification score."""
    return sum(SIGNAL_WEIGHTS.get(slug, 1) for slug in signals)


def _vendor_name(slug: str) -> str:
    vendor = VENDORS_BY_SLUG.get(slug)
    return vendor["name"] if vendor else slug


def _facts(attributes: dict[str, Any], record: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Everything an opener may draw on, normalised."""
    record = record or {}

    present: list[tuple[str, str]] = []
    for key, category in (
        ("review_vendors", "reviews"),
        ("email_vendors", "email"),
        ("chat_vendors", "chat"),
        ("loyalty_vendors", "loyalty"),
        ("sms_vendors", "sms"),
    ):
        for slug in attributes.get(key) or []:
            present.append((_vendor_name(slug), category))

    price = record.get("avg_price_usd")
    products = record.get("product_count")

    return {
        "present": present,
        "app": present[0][0] if present else None,
        "app_for": VENDOR_CATEGORY_LABEL.get(present[0][1]) if present else None,
        "pixel": bool(attributes.get("ad_pixel_present")),
        "products": int(products) if isinstance(products, (int, float)) else None,
        "price": round(float(price)) if isinstance(price, (int, float)) else None,
        "niche": (attributes.get("niche") or "").lower() or None,
        "theme": attributes.get("theme_name"),
        "paid_theme": attributes.get("is_free_theme") is False,
        "articles": attributes.get("blog_article_count"),
        "socials": attributes.get("social_links_count") or 0,
        "apps": attributes.get("app_count") or 0,
    }


# Each entry is (condition, writer). First match wins, so **order rules by how
# rare the triggering fact is** — rarest first.
#
# This is the same trap that broke maturity scoring: an attribute present on
# every store carries no information. A first draft put the ad-pixel rule near
# the top, and since 100% of discovered stores run a pixel it swallowed 5 of 7
# rows into one sentence. Universal facts belong at the bottom, just above the
# generic fallback.
Rule = tuple[Callable[[dict], bool], Callable[[dict], str]]

# Catalogue-size bands, so stores of different shapes get different framing.
def _size(f: dict) -> Optional[str]:
    n = f["products"]
    if not n:
        return None
    return "tiny" if n < 15 else "small" if n < 60 else "mid" if n < 150 else "large"

ANGLES: dict[str, list[Rule]] = {
    "no_email_marketing": [
        # Rarest first: a named complementary app, then price extremes, then
        # catalogue shape. The near-universal ad pixel sits second from bottom.
        (lambda f: bool(f["app"]) and f["app_for"] == "reviews",
         lambda f: f"You're collecting reviews through {f['app']}, but nothing on the storefront "
                   "captures an email — so the shoppers that social proof convinces still leave "
                   "with no way to reach them again."),
        (lambda f: bool(f["app"]) and f["app_for"] == "live chat",
         lambda f: f"{f['app']} is there to answer questions, but there's no email capture behind "
                   "it — so a conversation with a shopper who isn't ready today ends for good."),
        (lambda f: bool(f["price"]) and f["price"] >= 120,
         lambda f: f"At around ${f['price']} an order this is a considered purchase people research "
                   "before they commit, and there's no email capture to stay in front of them "
                   "while they do."),
        (lambda f: _size(f) == "large",
         lambda f: f"You're running a {f['products']}-product catalogue with no email capture, so "
                   "there's no way to put the right item back in front of anyone who browsed and "
                   "left."),
        (lambda f: _size(f) == "tiny" and bool(f["price"]) and f["price"] >= 80,
         lambda f: f"A focused range at ${f['price']} an order usually lives or dies on repeat "
                   "purchase, and nothing on the site is collecting an email to make that happen."),
        (lambda f: _size(f) == "mid",
         lambda f: f"With {f['products']} products live and no email capture on the site, browsing "
                   "shoppers have no way back in once they close the tab."),
        (lambda f: bool(f["price"]) and f["price"] <= 30,
         lambda f: f"At around ${f['price']} an order the first sale rarely pays for itself — but "
                   "there's no email capture running to earn the second one."),
        (lambda f: _size(f) == "small",
         lambda f: f"A tight {f['products']}-product range gives you a clear story to tell, and no "
                   "email capture on the storefront to tell it to anyone twice."),
        (lambda f: f["pixel"],
         lambda f: "Your ad pixel is live, so traffic is being paid for, but no email capture is "
                   "running on the storefront — that spend converts once or not at all."),
        (lambda f: True,
         lambda f: "There's no email capture running on the storefront, so the traffic you already "
                   "earn leaves without any way to bring it back."),
    ],
    "no_sms_marketing": [
        (lambda f: bool(f["app"]) and f["app_for"] == "email capture",
         lambda f: f"{f['app']} is handling your email, but there's no SMS capture alongside it — "
                   "usually the cheapest channel to add once email is already working."),
        (lambda f: bool(f["price"]) and f["price"] <= 45,
         lambda f: f"At around ${f['price']} an order, repeat purchase is where the margin is, and "
                   "SMS is the fastest way to prompt it — nothing is running on the storefront."),
        (lambda f: True,
         lambda f: "There's no SMS capture on the storefront — the one channel that still gets "
                   "opened reliably."),
    ],
    "no_reviews": [
        (lambda f: bool(f["price"]) and f["price"] >= 70,
         lambda f: f"At around ${f['price']} an order you're asking for real consideration, but no "
                   "review widget is loading on your product pages — buyers have nothing to "
                   "reassure them at the moment it matters."),
        (lambda f: bool(f["products"]) and f["products"] >= 60,
         lambda f: f"None of your {f['products']} product pages are loading a review widget, so "
                   "every one of them asks a first-time buyer to take your word for it."),
        (lambda f: bool(f["app"]) and f["app_for"] == "email capture",
         lambda f: f"You're running {f['app']} for email, so the retention side is covered — but no "
                   "review widget is loading on the product pages, which is what does most of the "
                   "convincing before the first order."),
        (lambda f: True,
         lambda f: "No review widget is loading on your product pages — you're missing the social "
                   "proof that does most of the convincing."),
    ],
    "no_loyalty_program": [
        (lambda f: bool(f["price"]) and f["price"] <= 50,
         lambda f: f"At around ${f['price']} an order the second purchase is where this becomes "
                   "profitable, and nothing is running to prompt it."),
        (lambda f: f["apps"] >= 3,
         lambda f: f"You've got {f['apps']} apps running on the storefront but nothing that brings a "
                   "first-time buyer back for a second order."),
        (lambda f: True,
         lambda f: "Nothing on the storefront is set up to bring a first-time buyer back for a "
                   "second order."),
    ],
    "no_ad_pixel": [
        (lambda f: True,
         lambda f: "There's no advertising pixel on the site, so any paid traffic you run can't be "
                   "measured or retargeted."),
    ],
    "no_blog_content": [
        (lambda f: f["articles"] == 0,
         lambda f: "Your sitemap lists no published articles at all, so the store depends entirely "
                   "on paid and direct traffic for discovery."),
        (lambda f: True,
         lambda f: f"Your sitemap lists only {f['articles']} published article(s) — not enough "
                   "cadence for search to treat the store as a source worth ranking."),
    ],
    "weak_seo_meta": [
        (lambda f: f["socials"] >= 3,
         lambda f: "You're active on several social channels, but key pages are missing the meta "
                   "and OG tags that decide how those links render when someone shares them."),
        (lambda f: True,
         lambda f: "Several key pages are missing meta descriptions and OG tags, which costs "
                   "click-through from both search and social shares."),
    ],
    "no_live_chat": [
        (lambda f: bool(f["price"]) and f["price"] >= 80,
         lambda f: f"At around ${f['price']} an order buyers usually have a question before they "
                   "commit, and there's no chat widget for them to ask it."),
        (lambda f: True,
         lambda f: "There's no chat widget on the storefront, so pre-purchase questions have "
                   "nowhere to go."),
    ],
}


def build_angle(
    signals: dict[str, Any],
    attributes: dict[str, Any],
    record: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Return a first-line opener for the highest-value gap on this store."""
    primary = next((slug for slug in ANGLE_PRIORITY if slug in signals), None)
    if not primary:
        return None

    facts = _facts(attributes, record)
    for condition, writer in ANGLES.get(primary, []):
        try:
            if condition(facts):
                return writer(facts)
        except (TypeError, ValueError, KeyError):
            continue
    return None


def summarize_evidence(signals: dict[str, Any]) -> dict[str, Any]:
    """Compact, buyer-facing evidence: what we checked and what we saw.

    This is what makes a row verifiable in seconds — the thing no competitor
    ships, and the reason our precision bar has to be higher than theirs.
    """
    return {
        slug: {
            "confidence": payload.get("confidence"),
            **{k: v for k, v in (payload.get("evidence") or {}).items()
               if k in ("checked_vendors", "faults", "blog_article_count",
                        "direct_pixels", "web_pixel_count", "caveat", "source")},
        }
        for slug, payload in signals.items()
    }
