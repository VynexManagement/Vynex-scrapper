from typing import Any, Callable, Iterable, Optional
from signal_dependencies import SIGNAL_DEPENDENCIES

DYNAMIC_DEPENDENCIES: dict[str, list[str]] = {}

# ── Confidence Scores Mapping ───────────────────────────────────────────────

DEFAULT_CONFIDENCES: dict[str, float] = {
    # Base Signals
    "no_email_detected": 0.85,
    "no_reviews_detected": 0.85,
    "no_social_links": 0.90,
    "no_trust_badges": 0.80,
    "no_loyalty_program": 0.80,
    "no_live_chat": 0.85,
    "no_upsell_tools": 0.80,
    "seo_weak": 0.95,
    "no_faq": 0.90,
    "no_content_marketing": 0.90,
    "no_brand_story": 0.90,
    "weak_contact_presence": 0.90,
    "missing_shipping_policy": 0.95,
    "missing_refund_policy": 0.95,
    "dead_store": 0.95,
    
    # Derived Signals
    "retention_opportunity": 0.80,
    "conversion_optimization_opportunity": 0.80,
    "agency_goldmine": 0.85,
    "app_install_target": 0.80,
    "premium_growth_gap": 0.85,
    "trust_gap": 0.85,
    "revenue_leakage": 0.80,
    "high_value_underserved": 0.85,
    "under_optimized_store": 0.80,
    "high_sku_no_email": 0.85,
    "premium_no_email": 0.85,
}

# ── Evidence Collection Helpers ──────────────────────────────────────────────

def get_base_signal_evidence(slug: str, raw_data: dict[str, Any]) -> dict[str, Any]:
    attrs = raw_data.get("attributes", {})
    if slug == "no_email_detected":
        return {"email_provider": attrs.get("email_provider")}
    elif slug == "no_reviews_detected":
        return {"review_provider": attrs.get("review_provider")}
    elif slug == "no_social_links":
        return {"social_links_count": attrs.get("social_links_count", 0)}
    elif slug == "no_trust_badges":
        return {"trust_provider": attrs.get("trust_provider")}
    elif slug == "no_loyalty_program":
        return {"loyalty_provider": attrs.get("loyalty_provider")}
    elif slug == "no_live_chat":
        return {"chat_provider": attrs.get("chat_provider")}
    elif slug == "no_upsell_tools":
        return {"upsell_provider": attrs.get("upsell_provider")}
    elif slug == "seo_weak":
        return {
            "has_meta_desc": attrs.get("has_meta_desc", False),
            "has_og_title": attrs.get("has_og_title", False)
        }
    elif slug == "no_faq":
        return {"has_faq": attrs.get("has_faq", False)}
    elif slug == "no_content_marketing":
        return {"has_blog": attrs.get("has_blog", False)}
    elif slug == "no_brand_story":
        return {"has_about_page": attrs.get("has_about_page", False)}
    elif slug == "weak_contact_presence":
        return {"has_contact": attrs.get("has_contact", False)}
    elif slug == "missing_shipping_policy":
        return {"has_shipping_policy": attrs.get("has_shipping_policy", False)}
    elif slug == "missing_refund_policy":
        return {"has_refund_policy": attrs.get("has_refund_policy", False)}
    elif slug == "dead_store":
        return {"is_dead": raw_data.get("is_dead", False)}
    return {}


def get_derived_signal_evidence(slug: str, base_signals: set[str], raw_data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    deps = DYNAMIC_DEPENDENCIES.get(slug)
    if deps is None:
        deps = SIGNAL_DEPENDENCIES.get(slug, [])
    triggered_deps = [dep for dep in deps if dep in base_signals]
    evidence = {"triggered_dependencies": triggered_deps}
    
    if raw_data:
        evidence.update({
            "product_count": raw_data.get("product_count"),
            "avg_price": raw_data.get("avg_price")
        })
        
    if slug == "under_optimized_store":
        evidence["evaluated_signals"] = [s for s in [
            "no_email_detected", "no_reviews_detected",
            "no_trust_badges", "no_social_links"
        ] if s in base_signals]
    elif slug == "high_sku_no_email":
        evidence["triggered_dependencies"] = ["no_email_detected"]
    elif slug == "premium_no_email":
        evidence["triggered_dependencies"] = ["no_email_detected"]
    elif slug == "high_value_underserved":
        evidence["triggered_dependencies"] = ["no_email_detected", "no_reviews_detected"]
        
    return evidence

# ── Base Rules ───────────────────────────────────────────────────────────────

def no_email_rule(raw_data: dict[str, Any]) -> bool:
    attrs = raw_data.get("attributes", {})
    return attrs.get("email_provider") is None


def no_reviews_rule(raw_data: dict[str, Any]) -> bool:
    attrs = raw_data.get("attributes", {})
    return attrs.get("review_provider") is None


def no_social_rule(raw_data: dict[str, Any]) -> bool:
    attrs = raw_data.get("attributes", {})
    return attrs.get("social_links_count", 0) == 0


def no_trust_rule(raw_data: dict[str, Any]) -> bool:
    attrs = raw_data.get("attributes", {})
    return attrs.get("trust_provider") is None


def no_loyalty_rule(raw_data: dict[str, Any]) -> bool:
    attrs = raw_data.get("attributes", {})
    return attrs.get("loyalty_provider") is None


def no_live_chat_rule(raw_data: dict[str, Any]) -> bool:
    attrs = raw_data.get("attributes", {})
    return attrs.get("chat_provider") is None


def no_upsell_rule(raw_data: dict[str, Any]) -> bool:
    attrs = raw_data.get("attributes", {})
    return attrs.get("upsell_provider") is None


def seo_weak_rule(raw_data: dict[str, Any]) -> bool:
    attrs = raw_data.get("attributes", {})
    return not attrs.get("has_meta_desc", False) and not attrs.get("has_og_title", False)


def no_faq_rule(raw_data: dict[str, Any]) -> bool:
    attrs = raw_data.get("attributes", {})
    return not attrs.get("has_faq", False)


def no_content_marketing_rule(raw_data: dict[str, Any]) -> bool:
    attrs = raw_data.get("attributes", {})
    return not attrs.get("has_blog", False)


def no_brand_story_rule(raw_data: dict[str, Any]) -> bool:
    attrs = raw_data.get("attributes", {})
    return not attrs.get("has_about_page", False)


def weak_contact_presence_rule(raw_data: dict[str, Any]) -> bool:
    attrs = raw_data.get("attributes", {})
    return not attrs.get("has_contact", False)


def missing_shipping_policy_rule(raw_data: dict[str, Any]) -> bool:
    attrs = raw_data.get("attributes", {})
    return not attrs.get("has_shipping_policy", False)


def missing_refund_policy_rule(raw_data: dict[str, Any]) -> bool:
    attrs = raw_data.get("attributes", {})
    return not attrs.get("has_refund_policy", False)


def dead_store_rule(raw_data: dict[str, Any]) -> bool:
    if raw_data.get("is_dead"):
        return True
    html = (raw_data.get("html") or "").lower()
    if not html:
        return False
    from fetcher import DEAD_STORE_MARKERS
    return any(m in html for m in DEAD_STORE_MARKERS)


BASE_RULES: dict[str, Callable[[dict[str, Any]], bool]] = {
    "no_email_detected":   no_email_rule,
    "no_reviews_detected": no_reviews_rule,
    "no_social_links":     no_social_rule,
    "no_trust_badges":     no_trust_rule,
    "no_loyalty_program":  no_loyalty_rule,
    "no_live_chat":        no_live_chat_rule,
    "no_upsell_tools":     no_upsell_rule,
    "seo_weak":            seo_weak_rule,
    "no_faq":              no_faq_rule,
    "no_content_marketing": no_content_marketing_rule,
    "no_brand_story":      no_brand_story_rule,
    "weak_contact_presence": weak_contact_presence_rule,
    "missing_shipping_policy": missing_shipping_policy_rule,
    "missing_refund_policy": missing_refund_policy_rule,
    "dead_store":          dead_store_rule,
}

# ── Dependency Evaluation ────────────────────────────────────────────────────

def evaluate_dependencies(derived_slug: str, base_signals: set[str]) -> bool:
    deps = DYNAMIC_DEPENDENCIES.get(derived_slug)
    if deps is None:
        deps = SIGNAL_DEPENDENCIES.get(derived_slug)
    if not deps:
        return False
    return all(dep in base_signals for dep in deps)

# ── Derived Rules ────────────────────────────────────────────────────────────

def under_optimized_rule(base_signals: set[str], raw_data: dict[str, Any]) -> bool:
    _ = raw_data
    score = sum(
        [
            "no_email_detected" in base_signals,
            "no_reviews_detected" in base_signals,
            "no_trust_badges" in base_signals,
            "no_social_links" in base_signals,
        ]
    )
    return score >= 2


def high_sku_no_email_rule(base_signals: set[str], raw_data: dict[str, Any]) -> bool:
    product_count = raw_data.get("product_count") or 0
    return product_count > 50 and "no_email_detected" in base_signals


def premium_no_email_rule(base_signals: set[str], raw_data: dict[str, Any]) -> bool:
    avg_price = raw_data.get("avg_price") or 0
    product_count = raw_data.get("product_count") or 0
    return (
        avg_price > 80
        and product_count > 5
        and "no_email_detected" in base_signals
    )


def high_value_underserved_rule(base_signals: set[str], raw_data: dict[str, Any]) -> bool:
    avg_price = raw_data.get("avg_price") or 0
    product_count = raw_data.get("product_count") or 0
    return (
        avg_price > 80
        and product_count > 10
        and "no_email_detected" in base_signals
        and "no_reviews_detected" in base_signals
    )


def retention_opportunity_rule(base_signals: set[str], raw_data: dict[str, Any]) -> bool:
    return evaluate_dependencies("retention_opportunity", base_signals)


def conversion_optimization_opportunity_rule(base_signals: set[str], raw_data: dict[str, Any]) -> bool:
    return evaluate_dependencies("conversion_optimization_opportunity", base_signals)


def agency_goldmine_rule(base_signals: set[str], raw_data: dict[str, Any]) -> bool:
    return evaluate_dependencies("agency_goldmine", base_signals)


def app_install_target_rule(base_signals: set[str], raw_data: dict[str, Any]) -> bool:
    return evaluate_dependencies("app_install_target", base_signals)


def premium_growth_gap_rule(base_signals: set[str], raw_data: dict[str, Any]) -> bool:
    avg_price = raw_data.get("avg_price") or 0
    product_count = raw_data.get("product_count") or 0
    return (
        avg_price > 80
        and product_count > 5
        and evaluate_dependencies("premium_growth_gap", base_signals)
    )


def trust_gap_rule(base_signals: set[str], raw_data: dict[str, Any]) -> bool:
    return evaluate_dependencies("trust_gap", base_signals)


def revenue_leakage_rule(base_signals: set[str], raw_data: dict[str, Any]) -> bool:
    return evaluate_dependencies("revenue_leakage", base_signals)


DERIVED_RULES: dict[str, Callable[[set[str], dict[str, Any]], bool]] = {
    "under_optimized_store":                under_optimized_rule,
    "high_sku_no_email":                    high_sku_no_email_rule,
    "premium_no_email":                     premium_no_email_rule,
    "high_value_underserved":               high_value_underserved_rule,
    "retention_opportunity":                retention_opportunity_rule,
    "conversion_optimization_opportunity":  conversion_optimization_opportunity_rule,
    "agency_goldmine":                      agency_goldmine_rule,
    "app_install_target":                   app_install_target_rule,
    "premium_growth_gap":                   premium_growth_gap_rule,
    "trust_gap":                            trust_gap_rule,
    "revenue_leakage":                      revenue_leakage_rule,
}


def generate_base_signals(raw_data: dict[str, Any], active_signals: Iterable[Any]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for signal in active_signals:
        if signal.slug in BASE_RULES and BASE_RULES[signal.slug](raw_data):
            results[signal.slug] = get_base_signal_evidence(signal.slug, raw_data)
    return results


def generate_derived_signals(
    base_signals: set[str], raw_data: dict[str, Any], active_signals: Iterable[Any]
) -> dict[str, Any]:
    # Update DYNAMIC_DEPENDENCIES from DB-loaded active signals
    for sig in active_signals:
        deps = getattr(sig, "dependencies", None)
        if deps is not None:
            DYNAMIC_DEPENDENCIES[sig.slug] = deps

    results: dict[str, Any] = {}
    for signal in active_signals:
        if signal.slug in DERIVED_RULES and DERIVED_RULES[signal.slug](base_signals, raw_data):
            results[signal.slug] = get_derived_signal_evidence(signal.slug, base_signals, raw_data)
    return results



NICHE_KEYWORDS: dict[str, list[str]] = {
    "Beauty":      ["skincare", "serum", "moisturizer", "foundation", "lipstick",
                    "concealer", "spf", "toner", "cleanser", "blush", "mascara"],
    "Fashion":     ["dress", "jeans", "hoodie", "sneakers", "blouse", "outfit",
                    "apparel", "clothing", "shirt", "jacket", "skirt", "tshirt"],
    "Fitness":     ["protein", "pre-workout", "gym", "resistance band", "supplement",
                    "whey", "creatine", "workout", "fitness", "dumbbell", "yoga"],
    "Jewelry":     ["necklace", "ring", "bracelet", "earring", "pendant",
                    "gold", "silver", "gemstone", "diamond", "charm"],
    "Pets":        ["dog", "cat", "puppy", "kitten", "pet food", "leash",
                    "paw", "fur", "collar", "treats", "aquarium"],
    "Home":        ["candle", "pillow", "blanket", "wall art", "decor",
                    "furniture", "lamp", "rug", "curtain", "bedding"],
    "Electronics": ["charger", "cable", "bluetooth", "wireless", "earbuds",
                    "phone case", "adapter", "battery", "usb", "speaker"],
    "Sports":      ["jersey", "cleats", "racket", "ball", "helmet",
                    "yoga mat", "running", "cycling", "swimming", "athletic"],
}

COUNTRY_SIGNALS: dict[str, list[str]] = {
    "USA":       ["$", "usd", "united states", "free us shipping", "ships to usa"],
    "India":     ["₹", "inr", "india", "rupee", "ships to india"],
    "UK":        ["£", "gbp", "united kingdom", "free uk shipping"],
    "Canada":    ["cad", "canada", "free canadian shipping"],
    "Australia": ["aud", "australia", "free aus shipping"],
}


def classify_niche(raw_data: dict[str, Any]) -> Optional[str]:
    """
    Infers niche from product titles, product types, and first 5KB of HTML.
    Used only when niche='all' — specific niche scrapes don't need this.
    Returns the best matching niche or None if no match found.
    """
    products = (raw_data.get("json_products") or {}).get("products", [])
    product_text = " ".join(
        (p.get("title") or "") + " " +
        (p.get("product_type") or "") + " " +
        (" ".join(p.get("tags") or []) if isinstance(p.get("tags"), list) else (p.get("tags") or ""))
        for p in products
    ).lower()

    html_snippet = (raw_data.get("html") or "")[:5000].lower()
    combined = product_text + " " + html_snippet

    scores: dict[str, int] = {}
    for niche, keywords in NICHE_KEYWORDS.items():
        scores[niche] = sum(combined.count(kw) for kw in keywords)

    if not any(scores.values()):
        return None
    return max(scores, key=scores.get)


def classify_country(raw_data: dict[str, Any]) -> Optional[str]:
    """
    Infers country from currency symbols, shipping text, and domain hints
    in the first 10KB of HTML. Used only when country='all'.
    Returns the best matching country or None if no match found.
    """
    html_snippet = (raw_data.get("html") or "")[:10000].lower()

    scores: dict[str, int] = {}
    for country, signals in COUNTRY_SIGNALS.items():
        scores[country] = sum(html_snippet.count(s) for s in signals)

    if not any(scores.values()):
        return None
    return max(scores, key=scores.get)