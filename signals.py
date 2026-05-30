from typing import Any, Callable, Iterable


def no_email_rule(raw_data: dict[str, Any]) -> bool:
    html = (raw_data.get("html") or "").lower()
    providers = ["klaviyo", "mailchimp", "omnisend", "drip", "sendlane", "activecampaign"]
    return not any(provider in html for provider in providers)


def no_reviews_rule(raw_data: dict[str, Any]) -> bool:
    html = (raw_data.get("html") or "").lower()
    providers = ["judge.me", "loox", "yotpo", "stamped", "okendo", "reviews.io"]
    return not any(provider in html for provider in providers)


def no_social_rule(raw_data: dict[str, Any]) -> bool:
    html = (raw_data.get("html") or "").lower()
    social_tokens = ["instagram.com/", "facebook.com/", "tiktok.com/", "x.com/", "twitter.com/"]
    return not any(token in html for token in social_tokens)


def no_trust_rule(raw_data: dict[str, Any]) -> bool:
    html = (raw_data.get("html") or "").lower()
    trust_tokens = ["secure", "guarantee", "trusted", "ssl", "mcafee", "norton"]
    return not any(token in html for token in trust_tokens)


def low_product_rule(raw_data: dict[str, Any]) -> bool:
    product_count = raw_data.get("product_count")
    return product_count is not None and product_count < 10


def high_product_rule(raw_data: dict[str, Any]) -> bool:
    product_count = raw_data.get("product_count")
    return product_count is not None and product_count > 200


BASE_RULES: dict[str, Callable[[dict[str, Any]], bool]] = {
    "no_email_detected": no_email_rule,
    "no_reviews_detected": no_reviews_rule,
    "no_social_links": no_social_rule,
    "no_trust_badges": no_trust_rule,
    "low_product_count": low_product_rule,
    "high_product_count": high_product_rule,
}


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
    avg_price = raw_data.get("avg_price")
    product_count = raw_data.get("product_count") or 0
    return bool(avg_price and avg_price > 80 and product_count < 50 and "no_email_detected" in base_signals)


def dropshipping_rule(base_signals: set[str], raw_data: dict[str, Any]) -> bool:
    _ = base_signals
    avg_price = raw_data.get("avg_price")
    product_count = raw_data.get("product_count") or 0
    return bool(product_count > 100 and avg_price and avg_price < 30)


def no_social_growth_rule(base_signals: set[str], raw_data: dict[str, Any]) -> bool:
    product_count = raw_data.get("product_count") or 0
    return product_count > 20 and "no_social_links" in base_signals


DERIVED_RULES: dict[str, Callable[[set[str], dict[str, Any]], bool]] = {
    "under_optimized_store": under_optimized_rule,
    "high_sku_no_email": high_sku_no_email_rule,
    "premium_no_email": premium_no_email_rule,
    "dropshipping_likely": dropshipping_rule,
    "no_social_growth_gap": no_social_growth_rule,
}


def generate_base_signals(raw_data: dict[str, Any], active_signals: Iterable[Any]) -> set[str]:
    results: set[str] = set()
    for signal in active_signals:
        if signal.slug in BASE_RULES and BASE_RULES[signal.slug](raw_data):
            results.add(signal.slug)
    return results


def generate_derived_signals(
    base_signals: set[str], raw_data: dict[str, Any], active_signals: Iterable[Any]
) -> set[str]:
    results: set[str] = set()
    for signal in active_signals:
        if signal.slug in DERIVED_RULES and DERIVED_RULES[signal.slug](base_signals, raw_data):
            results.add(signal.slug)
    return results