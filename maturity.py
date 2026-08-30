"""Maturity scoring.

Gap signals only work after a maturity filter. StoreInspect's 534K-store study
put it bluntly: if 62.8% of Shopify stores lack email marketing, "no email app"
is not a filter — it is the whole market with a nicer label.

This is a **proxy**, and it should be described as one wherever it is shown. We
have no traffic data, unlike Store Leads and StoreInspect. What we do have is
the fingerprint we already extract, which carries several spend signals for free.
"""

from typing import Any, Optional

from config import (
    MATURITY_ESTABLISHED_APPS,
    MATURITY_GROWING_APPS,
    PREMIUM_AVG_PRICE_USD,
)

TIER_SEED = "seed"
TIER_GROWING = "growing"
TIER_ESTABLISHED = "established"


def score_maturity(
    attributes: dict[str, Any],
    product_count: Optional[int],
    avg_price_usd: Optional[float],
) -> dict[str, Any]:
    """Return {tier, score, components} — components double as evidence."""
    components: dict[str, Any] = {}
    score = 0

    app_count = attributes.get("app_count") or 0
    if app_count >= MATURITY_ESTABLISHED_APPS:
        score += 2
        components["app_count"] = f"{app_count} (>= {MATURITY_ESTABLISHED_APPS})"
    elif app_count >= MATURITY_GROWING_APPS:
        score += 1
        components["app_count"] = f"{app_count} (>= {MATURITY_GROWING_APPS})"
    else:
        components["app_count"] = str(app_count)

    # Ad pixel is recorded but scores ZERO.
    #
    # Measured on 127 sheet rows (2026-08-23): 100% of discovered stores carry
    # an ad pixel. A universal attribute carries no information — weighting it
    # merely shifted every store up by the same 2 points and pushed the whole
    # population into "established", which is why the tier stopped separating
    # anything. Kept as evidence, not as score.
    #
    # Note this is partly a sampling effect: discovery finds stores through
    # Google organic results, which skews toward stores that market themselves.
    # Revisit the weight if the discovery mix ever changes.
    components["ad_pixel"] = "present" if attributes.get("ad_pixel_present") else "absent"

    is_free_theme = attributes.get("is_free_theme")
    if is_free_theme is False:
        score += 1
        components["theme"] = f"paid ({attributes.get('theme_name')})"
    elif is_free_theme is True:
        components["theme"] = f"free ({attributes.get('theme_name')})"
    else:
        components["theme"] = "unknown"

    if product_count is not None:
        components["product_count"] = product_count
        if product_count >= 50:
            score += 2
        elif product_count >= 15:
            score += 1

    if avg_price_usd is not None:
        components["avg_price_usd"] = avg_price_usd
        if avg_price_usd >= PREMIUM_AVG_PRICE_USD:
            score += 1

    social = attributes.get("social_links_count") or 0
    components["social_links"] = social
    if social >= 3:
        score += 1

    # Thresholds recalibrated against the observed score distribution once the
    # universal ad-pixel weight was removed. Before: 0 seed / 44 growing /
    # 83 established, i.e. the tier told a buyer nothing.
    if score >= 4:
        tier = TIER_ESTABLISHED
    elif score >= 2:
        tier = TIER_GROWING
    else:
        tier = TIER_SEED

    return {
        "tier": tier,
        "score": score,
        "components": components,
        "basis": "proxy — no traffic data available",
    }
