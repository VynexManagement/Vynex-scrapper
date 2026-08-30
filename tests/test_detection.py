"""Regression tests for the detection rewrite.

Each test named `test_regression_*` pins a specific false positive or false
negative that the previous substring-scan implementation produced.
"""

import pytest

from fetcher import assess_quality
from fingerprint import build_fingerprint
from signals import generate_signals
from synthetic import (
    CHALLENGE_HTML,
    EMPTY_WEB_PIXELS_INLINE,
    WEB_PIXELS_INLINE,
    make_store_html,
)

BASE_URL = "https://northwind.example"


def score(html: str, blog_articles=None):
    fp = build_fingerprint(html, BASE_URL)
    if blog_articles is not None:
        fp["blog_article_count"] = blog_articles
    quality = assess_quality(html, fp, "httpx")
    signals, attributes = generate_signals(fp, quality)
    return signals, attributes, quality


# ── Quality gating ───────────────────────────────────────────────────────────

def test_challenge_page_produces_no_signals():
    signals, _, quality = score(CHALLENGE_HTML)
    assert quality["scoreable"] is False
    assert quality["reason"] == "bot_challenge"
    assert signals == {}, "a bot challenge must never yield gap signals"


def test_healthy_store_is_scoreable():
    _, _, quality = score(make_store_html())
    assert quality["scoreable"] is True


# ── Email ────────────────────────────────────────────────────────────────────

def test_klaviyo_detected_by_script_host():
    _, attrs, _ = score(make_store_html(
        script_srcs=["https://static.klaviyo.com/onsite/js/klaviyo.js?company_id=ABC"],
    ))
    assert "klaviyo" in attrs["email_vendors"]


def test_regression_privacy_does_not_match_privy():
    """`"privy" in html` fired on the word "privacy", present on nearly every
    store, silently suppressing our highest-volume signal."""
    signals, attrs, _ = score(make_store_html(
        body_text="Read our privacy policy and privacy notice for details.",
    ))
    assert attrs["email_vendors"] == []
    assert "no_email_marketing" in signals


def test_regression_drip_coffee_does_not_match_drip():
    signals, attrs, _ = score(make_store_html(
        body_text="Our slow drip coffee is dripping with flavour.",
    ))
    assert attrs["email_vendors"] == []
    assert "no_email_marketing" in signals


def test_drip_detected_by_real_script():
    _, attrs, _ = score(make_store_html(script_srcs=["https://tag.getdrip.com/1234.js"]))
    assert "drip" in attrs["email_vendors"]


# ── SMS suppression ──────────────────────────────────────────────────────────

def test_sms_signal_suppressed_when_klaviyo_present():
    """Klaviyo sells email and SMS from one script; asserting "no SMS" against a
    Klaviyo store produces a lead that dies on the first call."""
    signals, _, _ = score(make_store_html(
        script_srcs=["https://static.klaviyo.com/onsite/js/klaviyo.js"],
    ))
    assert "no_sms_marketing" not in signals


def test_sms_signal_fires_without_dual_vendor():
    signals, _, _ = score(make_store_html(script_srcs=["https://tag.getdrip.com/1234.js"]))
    assert "no_sms_marketing" in signals


def test_attentive_detected():
    _, attrs, _ = score(make_store_html(script_srcs=["https://cdn.attn.tv/brand/dtag.js"]))
    assert "attentive" in attrs["sms_vendors"]


# ── Ad pixel ─────────────────────────────────────────────────────────────────

def test_regression_shopify_sandboxed_pixels_count_as_present():
    """Since Shopify's customer-events migration many stores load no
    connect.facebook.net at all — pixels run inside web-pixels-manager. Checking
    only vendor hosts would flag the best-instrumented stores as having none."""
    signals, attrs, _ = score(make_store_html(inline_js=WEB_PIXELS_INLINE))
    assert attrs["ad_pixel_present"] is True
    assert "no_ad_pixel" not in signals


def test_no_pixel_when_web_pixel_list_empty_and_no_hosts():
    signals, attrs, _ = score(make_store_html(inline_js=EMPTY_WEB_PIXELS_INLINE))
    assert attrs["ad_pixel_present"] is False
    assert "no_ad_pixel" in signals


def test_direct_meta_pixel_detected():
    _, attrs, _ = score(make_store_html(
        script_srcs=["https://connect.facebook.net/en_US/fbevents.js"],
    ))
    assert attrs["ad_pixel_present"] is True


def test_shopify_own_analytics_is_not_an_ad_pixel():
    """Shopify.analytics ships on nearly every store and must not count."""
    signals, attrs, _ = score(make_store_html(
        inline_js='window.Shopify.analytics = {replayQueue:[]};',
    ))
    assert attrs["ad_pixel_present"] is False
    assert "no_ad_pixel" in signals


# ── Reviews ──────────────────────────────────────────────────────────────────

def test_regression_hand_stamped_does_not_match_stamped_io():
    signals, attrs, _ = score(make_store_html(
        body_text="Each hand-stamped jewelry piece is stamped by our artisans.",
    ))
    assert attrs["review_vendors"] == []
    assert "no_reviews" in signals


def test_judgeme_detected_by_host():
    signals, attrs, _ = score(make_store_html(
        script_srcs=["https://cdn.judge.me/widget_preloader.js"],
    ))
    assert "judgeme" in attrs["review_vendors"]
    assert "no_reviews" not in signals


def test_two_weak_signals_are_enough():
    """A widget class plus a matching global is sufficient evidence."""
    _, attrs, _ = score(make_store_html(
        class_names=["jdgm-widget", "jdgm-rev"],
        inline_js="var jdgm = {};",
    ))
    assert "judgeme" in attrs["review_vendors"]


def test_one_weak_signal_is_not_enough():
    _, attrs, _ = score(make_store_html(class_names=["loox-rating"]))
    assert "loox" not in attrs["review_vendors"]


# ── Chat and loyalty ─────────────────────────────────────────────────────────

def test_gorgias_detected():
    signals, attrs, _ = score(make_store_html(
        script_srcs=["https://config.gorgias.chat/bundle-loader/01ABC"],
    ))
    assert "gorgias" in attrs["chat_vendors"]
    assert "no_live_chat" not in signals


def test_smile_io_detected():
    signals, attrs, _ = score(make_store_html(script_srcs=["https://sdk.smile.io/v1/smile-ui.js"]))
    assert "smile_io" in attrs["loyalty_vendors"]
    assert "no_loyalty_program" not in signals


def test_bare_store_fires_all_vendor_gaps():
    signals, _, _ = score(make_store_html(inline_js=EMPTY_WEB_PIXELS_INLINE))
    for slug in ("no_email_marketing", "no_sms_marketing", "no_ad_pixel",
                 "no_reviews", "no_loyalty_program", "no_live_chat"):
        assert slug in signals


# ── Blog ─────────────────────────────────────────────────────────────────────

def test_blog_signal_fires_below_threshold():
    signals, _, _ = score(make_store_html(), blog_articles=1)
    assert "no_blog_content" in signals
    assert signals["no_blog_content"]["evidence"]["blog_article_count"] == 1


def test_blog_signal_silent_when_publishing():
    signals, _, _ = score(make_store_html(), blog_articles=12)
    assert "no_blog_content" not in signals


def test_blog_signal_silent_when_unknown():
    """Sitemap unreachable means unknown, not zero — never guess a gap."""
    signals, _, _ = score(make_store_html(), blog_articles=None)
    assert "no_blog_content" not in signals


# ── SEO ──────────────────────────────────────────────────────────────────────

def test_seo_signal_silent_on_well_formed_head():
    signals, _, _ = score(make_store_html())
    assert "weak_seo_meta" not in signals


def test_seo_signal_fires_on_multiple_faults():
    signals, _, _ = score(make_store_html(
        meta_description=None, og_title=None, h1_count=0, title="Home",
    ))
    assert "weak_seo_meta" in signals
    assert len(signals["weak_seo_meta"]["evidence"]["faults"]) >= 2


def test_seo_signal_needs_two_faults():
    """A single fault is normal variation, not a sellable gap."""
    signals, _, _ = score(make_store_html(og_title=None))
    assert "weak_seo_meta" not in signals


# ── Shopify app install surfaces ─────────────────────────────────────────────
# 83% of sampled real stores install apps as theme app extensions served from
# cdn.shopify.com, so host matching alone is blind to most modern installs.

def test_theme_app_extension_is_a_strong_match():
    _, attrs, _ = score(make_store_html(script_srcs=[
        "https://cdn.shopify.com/extensions/01a02574-6390-715b-9622-33ef4acfdaa1"
        "/judgeme-719/assets/loader.js",
    ]))
    assert "judgeme" in attrs["review_vendors"]


def test_app_embed_block_uri_is_detected():
    """Some apps appear only as shopify://apps/<handle>/blocks/... references."""
    html = make_store_html().replace(
        "<body>",
        "<body><!-- begin app block: shopify://apps/yotpo-loyalty-rewards"
        "/blocks/loader-app-embed-block/x -->",
    )
    _, attrs, _ = score(html)
    assert "yotpo_loyalty" in attrs["loyalty_vendors"]


def test_widget_class_inside_escaped_template_is_seen():
    """Judge.me badges live in JS template strings the DOM parser never sees."""
    _, attrs, _ = score(make_store_html(
        inline_js=r'var tpl = "<div class=\"jdgm-badge-placeholder\"></div>"; var jdgm = {};',
    ))
    assert "judgeme" in attrs["review_vendors"]


def test_raw_class_scan_does_not_fire_oke_inside_words():
    """`oke-` (Okendo) must not match inside smoke-, spoke-, broke-."""
    _, attrs, _ = score(make_store_html(
        body_text="Our smoke-free, spoke-driven, never-broke-down workshop.",
    ))
    assert "okendo" not in attrs["review_vendors"]


# ── First-party proxy (CNAME cloaking) ───────────────────────────────────────

def _big_proxied_store(**kwargs):
    """A large storefront whose only scripts are first-party or Shopify infra."""
    return make_store_html(
        script_srcs=[
            "https://khmms.northwind.example/analytics.js",
            "https://cdn.shopify.com/s/files/1/assets/theme.js",
        ],
        padding=2400,
        **kwargs,
    )


def test_first_party_proxy_is_detected():
    from detectors import build_attributes
    from fingerprint import build_fingerprint
    fp = build_fingerprint(_big_proxied_store(), BASE_URL)
    attrs = build_attributes(fp)
    assert attrs["first_party_proxy_suspected"] is True


def test_proxied_store_suppresses_vendor_signals():
    """A brand proxying vendor scripts through its own subdomain hides the
    vendor host, so absence stops being evidence. Better to say nothing than to
    ship a confident false positive a buyer can see through."""
    signals, _, quality = score(_big_proxied_store())
    assert quality["scoreable"] is True
    for slug in ("no_email_marketing", "no_reviews", "no_live_chat",
                 "no_loyalty_program", "no_sms_marketing", "no_ad_pixel"):
        assert slug not in signals


def test_proxied_store_still_reports_structural_signals():
    """Blog and SEO never depended on vendor hosts, so they survive."""
    signals, _, _ = score(
        _big_proxied_store(meta_description=None, og_title=None, h1_count=0, title="Home"),
        blog_articles=0,
    )
    assert "no_blog_content" in signals
    assert "weak_seo_meta" in signals


def test_normal_store_is_not_flagged_as_proxied():
    from detectors import build_attributes
    from fingerprint import build_fingerprint
    fp = build_fingerprint(make_store_html(
        script_srcs=["https://cdn.judge.me/widget_preloader.js"], padding=2400,
    ), BASE_URL)
    assert build_attributes(fp)["first_party_proxy_suspected"] is False


# ── Confidence ───────────────────────────────────────────────────────────────

def test_confidence_is_computed_not_constant():
    """A store where we positively identified vendors should yield higher
    confidence in the remaining gaps than a bare one."""
    rich, _, _ = score(make_store_html(
        script_srcs=[
            "https://cdn.judge.me/widget_preloader.js",
            "https://sdk.smile.io/v1/smile-ui.js",
            "https://connect.facebook.net/en_US/fbevents.js",
        ],
    ))
    bare, _, _ = score(make_store_html(inline_js=EMPTY_WEB_PIXELS_INLINE))
    assert rich["no_email_marketing"]["confidence"] > bare["no_email_marketing"]["confidence"]


def test_confidence_never_exceeds_cap():
    signals, _, _ = score(make_store_html(
        script_srcs=["https://cdn.judge.me/widget_preloader.js"],
    ))
    assert all(s["confidence"] <= 0.98 for s in signals.values())


# ── Fingerprint plumbing ─────────────────────────────────────────────────────

def test_fingerprint_captures_shopify_identity():
    fp = build_fingerprint(make_store_html(theme_name="Impulse", currency="AUD"), BASE_URL)
    assert fp["theme_name"] == "Impulse"
    assert fp["currency"] == "AUD"
    assert fp["shop_handle"] == "northwind-supply.myshopify.com"


def test_fingerprint_hash_is_stable_across_identical_pages():
    from fingerprint import fingerprint_hash
    a = build_fingerprint(make_store_html(), BASE_URL)
    b = build_fingerprint(make_store_html(), BASE_URL)
    assert fingerprint_hash(a) == fingerprint_hash(b)


def test_fingerprint_hash_changes_when_a_vendor_appears():
    from fingerprint import fingerprint_hash
    before = build_fingerprint(make_store_html(), BASE_URL)
    after = build_fingerprint(
        make_store_html(script_srcs=["https://cdn.judge.me/widget_preloader.js"]), BASE_URL
    )
    assert fingerprint_hash(before) != fingerprint_hash(after)


@pytest.mark.parametrize("currency,expected", [("AUD", "Australia"), ("USD", "USA")])
def test_country_classification_uses_currency(currency, expected):
    from signals import classify_country
    fp = build_fingerprint(make_store_html(currency=currency), BASE_URL)
    assert classify_country(fp) == expected


# ── Opener variety ───────────────────────────────────────────────────────────
# The opener is the product: personalised outreach replies at 5-8% against under
# 1% generic. An earlier version emitted byte-identical sentences on 4 of 14 rows.

def test_same_gap_different_store_gives_different_opener():
    from outreach import build_angle
    signals = {"no_email_marketing": {"confidence": 0.9, "evidence": {}}}
    attrs = {"email_vendors": [], "review_vendors": ["judgeme"], "chat_vendors": [],
             "loyalty_vendors": [], "sms_vendors": [], "ad_pixel_present": True,
             "app_count": 2, "social_links_count": 1}
    small = build_angle(signals, {**attrs, "review_vendors": []},
                        {"product_count": 8, "avg_price_usd": 120.0})
    large = build_angle(signals, {**attrs, "review_vendors": []},
                        {"product_count": 180, "avg_price_usd": 22.0})
    with_reviews = build_angle(signals, attrs, {"product_count": 40, "avg_price_usd": 45.0})
    assert len({small, large, with_reviews}) == 3, "same gap must not yield one sentence"
    assert all(a for a in (small, large, with_reviews))


def test_opener_cites_a_concrete_detail():
    from outreach import build_angle
    angle = build_angle(
        {"no_reviews": {"confidence": 0.9, "evidence": {}}},
        {"email_vendors": [], "review_vendors": [], "chat_vendors": [], "loyalty_vendors": [],
         "sms_vendors": [], "ad_pixel_present": False, "app_count": 1, "social_links_count": 0},
        {"product_count": 140, "avg_price_usd": 30.0},
    )
    assert "140" in angle


def test_opener_falls_back_when_no_facts_available():
    from outreach import build_angle
    angle = build_angle(
        {"no_live_chat": {"confidence": 0.9, "evidence": {}}},
        {"email_vendors": [], "review_vendors": [], "chat_vendors": [], "loyalty_vendors": [],
         "sms_vendors": [], "ad_pixel_present": False, "app_count": 0, "social_links_count": 0},
        None,
    )
    assert angle and "chat widget" in angle


def test_signal_score_weights_scarce_gaps_higher():
    from outreach import signal_score
    scarce = signal_score({"no_email_marketing": {}, "no_sms_marketing": {}})
    common = signal_score({"no_loyalty_program": {}, "no_live_chat": {}})
    assert scarce > common * 4
