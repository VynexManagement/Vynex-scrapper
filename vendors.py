"""Vendor registry — pure data.

Adding or correcting a vendor is a data change here, never a code change in the
detector. Matching is two-tier:

  strong  a host match on a <script src> or <link href>. Near-zero false
          positive rate: prose cannot contain a hostname in a src attribute.
  weak    a global JS identifier or a widget CSS class prefix. One weak match
          alone is not enough; two are.

This replaces the old `key in html.lower()` substring scan, which fired
"drip" on "drip coffee", "privy" on the word "privacy" (present on essentially
every store with a privacy policy), and "stamped" on "hand-stamped jewelry".
"""

from typing import Any

# Categories map 1:1 onto the gap signals, except `blog`/`seo` which are
# structural rather than vendor-based.
CATEGORY_EMAIL = "email"
CATEGORY_SMS = "sms"
CATEGORY_AD_PIXEL = "ad_pixel"
CATEGORY_REVIEWS = "reviews"
CATEGORY_LOYALTY = "loyalty"
CATEGORY_CHAT = "chat"

CATEGORIES = [
    CATEGORY_EMAIL,
    CATEGORY_SMS,
    CATEGORY_AD_PIXEL,
    CATEGORY_REVIEWS,
    CATEGORY_LOYALTY,
    CATEGORY_CHAT,
]


# `sells_sms` marks vendors whose email product also sells SMS. The storefront
# cannot tell which product a merchant actually pays for, so the presence of one
# of these suppresses `no_sms_marketing` rather than asserting a gap that would
# die on the first sales call.
VENDORS: list[dict[str, Any]] = [
    # ── Email marketing ─────────────────────────────────────────────────────
    {
        "slug": "klaviyo",
        "name": "Klaviyo",
        "category": CATEGORY_EMAIL,
        "sells_sms": True,
        "hosts": ["static.klaviyo.com", "static-tracking.klaviyo.com", "a.klaviyo.com"],
        "extensions": ['klaviyo'],
        "globals": ["_learnq", "klaviyo"],
        "classes": ["klaviyo-form"],
    },
    {
        "slug": "omnisend",
        "name": "Omnisend",
        "category": CATEGORY_EMAIL,
        "sells_sms": True,
        "hosts": ["omnisnippet1.com", "omnisend.com"],
        "extensions": ['omnisend'],
        "globals": ["omnisend"],
        "classes": [],
    },
    {
        "slug": "mailchimp",
        "name": "Mailchimp",
        "category": CATEGORY_EMAIL,
        "hosts": ["chimpstatic.com", "list-manage.com", "mailchimp.com"],
        "extensions": ['mailchimp'],
        "globals": ["mcjs"],
        "classes": ["mc4wp", "mc-field-group"],
    },
    {
        "slug": "drip",
        "name": "Drip",
        "category": CATEGORY_EMAIL,
        "hosts": ["tag.getdrip.com", "getdrip.com"],
        "extensions": ['drip'],
        "globals": ["_dcq", "_dcs"],
        "classes": [],
    },
    {
        "slug": "sendlane",
        "name": "Sendlane",
        "category": CATEGORY_EMAIL,
        "sells_sms": True,
        "hosts": ["cdn.sendlane.com", "sendlane.com"],
        "extensions": ['sendlane'],
        "globals": ["sendlane"],
        "classes": [],
    },
    {
        "slug": "activecampaign",
        "name": "ActiveCampaign",
        "category": CATEGORY_EMAIL,
        "hosts": ["prism.app-us1.com", "trackcmp.net", "activehosted.com"],
        "extensions": ['activecampaign'],
        "globals": ["vgo"],
        "classes": [],
    },
    {
        "slug": "privy",
        "name": "Privy",
        "category": CATEGORY_EMAIL,
        "hosts": ["widget.privy.com", "privy.com", "privymktg.com"],
        "extensions": ['privy'],
        "globals": ["privy"],
        "classes": ["privy-"],
    },

    # ── SMS marketing ───────────────────────────────────────────────────────
    {
        "slug": "attentive",
        "name": "Attentive",
        "category": CATEGORY_SMS,
        "hosts": ["cdn.attn.tv", "attn.tv", "attentivemobile.com"],
        "extensions": ['attentive'],
        "globals": ["__attentive", "attentive"],
        "classes": ["attentive_"],
    },
    {
        "slug": "postscript",
        "name": "Postscript",
        "category": CATEGORY_SMS,
        "hosts": ["sdk.postscript.io", "postscript.io"],
        "extensions": ['postscript'],
        "globals": ["postscript"],
        "classes": [],
    },
    {
        "slug": "yotpo_sms",
        "name": "Yotpo SMS (SMSBump)",
        "category": CATEGORY_SMS,
        "hosts": ["cdn-widgetsrepository.yotpo.com", "smsbump.com"],
        "extensions": ['smsbump', 'yotpo-sms'],
        "globals": ["smsbump"],
        "classes": [],
    },
    {
        "slug": "recart",
        "name": "Recart",
        "category": CATEGORY_SMS,
        "hosts": ["cdn.recart.com", "recart.com"],
        "extensions": ['recart'],
        "globals": ["recart"],
        "classes": [],
    },

    # ── Advertising / analytics pixels ──────────────────────────────────────
    # NOTE: Shopify's own `Shopify.analytics` is present on nearly every store
    # and is deliberately NOT listed — it is not an ad pixel.
    {
        "slug": "meta_pixel",
        "name": "Meta Pixel",
        "category": CATEGORY_AD_PIXEL,
        "hosts": ["connect.facebook.net", "facebook.com/tr"],
        "extensions": ['facebook', 'meta-pixel'],
        "globals": ["fbq", "_fbq"],
        "classes": [],
    },
    {
        "slug": "google_ads",
        "name": "Google Ads / GA4",
        "category": CATEGORY_AD_PIXEL,
        "hosts": ["googletagmanager.com", "google-analytics.com", "googleadservices.com"],
        "extensions": ['google'],
        "globals": ["gtag", "dataLayer", "ga"],
        "classes": [],
    },
    {
        "slug": "tiktok_pixel",
        "name": "TikTok Pixel",
        "category": CATEGORY_AD_PIXEL,
        "hosts": ["analytics.tiktok.com"],
        "extensions": ['tiktok'],
        "globals": ["ttq"],
        "classes": [],
    },
    {
        "slug": "pinterest_tag",
        "name": "Pinterest Tag",
        "category": CATEGORY_AD_PIXEL,
        "hosts": ["s.pinimg.com", "ct.pinterest.com"],
        "extensions": ['pinterest'],
        "globals": ["pintrk"],
        "classes": [],
    },
    {
        "slug": "snap_pixel",
        "name": "Snap Pixel",
        "category": CATEGORY_AD_PIXEL,
        "hosts": ["sc-static.net", "tr.snapchat.com"],
        "extensions": ['snapchat'],
        "globals": ["snaptr"],
        "classes": [],
    },

    # ── Reviews ─────────────────────────────────────────────────────────────
    {
        "slug": "judgeme",
        "name": "Judge.me",
        "category": CATEGORY_REVIEWS,
        "hosts": ["cdn.judge.me", "cdnwidget.judge.me", "judge.me"],
        "extensions": ['judgeme', 'judge-me'],
        "globals": ["jdgm"],
        "classes": ["jdgm-"],
    },
    {
        "slug": "loox",
        "name": "Loox",
        "category": CATEGORY_REVIEWS,
        "hosts": ["loox.io", "loox.app"],
        "extensions": ['loox'],
        "globals": ["loox"],
        "classes": ["loox-"],
    },
    {
        "slug": "yotpo_reviews",
        "name": "Yotpo Reviews",
        "category": CATEGORY_REVIEWS,
        "hosts": ["staticw2.yotpo.com", "yotpo.com"],
        "extensions": ['yotpo-reviews', 'yotpo'],
        "globals": ["yotpo"],
        "classes": ["yotpo"],
    },
    {
        "slug": "okendo",
        "name": "Okendo",
        "category": CATEGORY_REVIEWS,
        "hosts": ["okendo.io", "cdn-static.okendo.io"],
        "extensions": ['okendo'],
        "globals": ["okeReviews", "okendo"],
        "classes": ["oke-"],
    },
    {
        "slug": "stamped",
        "name": "Stamped.io",
        "category": CATEGORY_REVIEWS,
        "hosts": ["cdn1.stamped.io", "stamped.io"],
        "extensions": ['stamped'],
        "globals": ["StampedFn"],
        "classes": ["stamped-"],
    },
    {
        "slug": "reviews_io",
        "name": "Reviews.io",
        "category": CATEGORY_REVIEWS,
        "hosts": ["widget.reviews.io", "reviews.io", "reviews.co.uk"],
        "extensions": ['reviews-io'],
        "globals": ["reviewsBadgeRibbon"],
        "classes": ["ruk_"],
    },
    {
        "slug": "opinew",
        "name": "Opinew",
        "category": CATEGORY_REVIEWS,
        "hosts": ["opinew.com"],
        "extensions": ['opinew'],
        "globals": ["opinew"],
        "classes": ["opinew-"],
    },
    {
        "slug": "shopify_reviews",
        "name": "Shopify Product Reviews (legacy)",
        "category": CATEGORY_REVIEWS,
        "hosts": [],
        "extensions": ['product-reviews'],
        "globals": ["SPR"],
        "classes": ["spr-"],
    },

    # ── Loyalty ─────────────────────────────────────────────────────────────
    {
        "slug": "smile_io",
        "name": "Smile.io",
        "category": CATEGORY_LOYALTY,
        "hosts": ["sdk.smile.io", "smile.io"],
        "extensions": ['smile-io', 'smile'],
        "globals": ["SmileUI", "smile"],
        "classes": ["smile-"],
    },
    {
        "slug": "loyaltylion",
        "name": "LoyaltyLion",
        "category": CATEGORY_LOYALTY,
        "hosts": ["sdk.loyaltylion.com", "cdn.loyaltylion.com", "loyaltylion.com"],
        "extensions": ['loyaltylion'],
        "globals": ["loyaltylion"],
        "classes": ["lion-"],
    },
    {
        "slug": "growave",
        "name": "Growave",
        "category": CATEGORY_LOYALTY,
        "hosts": ["growave.io", "socialshopwave.com"],
        "extensions": ['growave'],
        "globals": ["ssw", "growave"],
        "classes": ["ssw-"],
    },
    {
        "slug": "rivo",
        "name": "Rivo",
        "category": CATEGORY_LOYALTY,
        "hosts": ["cdn.rivo.io", "rivo.io"],
        "extensions": ['rivo'],
        "globals": ["rivo"],
        "classes": ["rivo-"],
    },
    {
        "slug": "yotpo_loyalty",
        "name": "Yotpo Loyalty (Swell)",
        "category": CATEGORY_LOYALTY,
        "hosts": ["cdn-loyalty.yotpo.com", "swellrewards.com"],
        "extensions": ['swell', 'yotpo-loyalty', 'yotpo-loyalty-rewards'],
        "globals": ["swellAPI"],
        "classes": ["swell-"],
    },
    {
        "slug": "bon_loyalty",
        "name": "BON Loyalty",
        "category": CATEGORY_LOYALTY,
        "hosts": ["bonloyalty.com"],
        "extensions": ['bon-loyalty'],
        "globals": [],
        "classes": ["bon-"],
    },

    # ── Live chat / helpdesk ────────────────────────────────────────────────
    {
        "slug": "tidio",
        "name": "Tidio",
        "category": CATEGORY_CHAT,
        "hosts": ["code.tidio.co", "tidio.co", "tidiochat.com"],
        "extensions": ['tidio'],
        "globals": ["tidioChatApi"],
        "classes": [],
    },
    {
        "slug": "gorgias",
        "name": "Gorgias",
        "category": CATEGORY_CHAT,
        "hosts": ["config.gorgias.chat", "assets.gorgias.chat", "gorgias.chat"],
        "extensions": ['gorgias'],
        "globals": ["GorgiasChat"],
        "classes": ["gorgias-"],
    },
    {
        "slug": "intercom",
        "name": "Intercom",
        "category": CATEGORY_CHAT,
        "hosts": ["widget.intercom.io", "js.intercomcdn.com", "intercom.io"],
        "extensions": ['intercom'],
        "globals": ["Intercom", "intercomSettings"],
        "classes": ["intercom-"],
    },
    {
        "slug": "zendesk",
        "name": "Zendesk",
        "category": CATEGORY_CHAT,
        "hosts": ["static.zdassets.com", "zdassets.com", "zendesk.com"],
        "extensions": ['zendesk'],
        "globals": ["zE", "zEmbed"],
        "classes": [],
    },
    {
        "slug": "tawk",
        "name": "Tawk.to",
        "category": CATEGORY_CHAT,
        "hosts": ["embed.tawk.to", "tawk.to"],
        "extensions": ['tawk'],
        "globals": ["Tawk_API"],
        "classes": [],
    },
    {
        "slug": "crisp",
        "name": "Crisp",
        "category": CATEGORY_CHAT,
        "hosts": ["client.crisp.chat", "crisp.chat"],
        "extensions": ['crisp'],
        "globals": ["$crisp", "CRISP_WEBSITE_ID"],
        "classes": ["crisp-"],
    },
    {
        "slug": "reamaze",
        "name": "Re:amaze",
        "category": CATEGORY_CHAT,
        "hosts": ["cdn.reamaze.com", "reamaze.com"],
        "extensions": ['reamaze'],
        "globals": ["_support"],
        "classes": ["reamaze-"],
    },
    {
        "slug": "shopify_inbox",
        "name": "Shopify Inbox",
        "category": CATEGORY_CHAT,
        "hosts": ["chat.shopify.com"],
        "extensions": ['inbox'],
        "globals": [],
        "classes": ["shopify-chat"],
    },
    # ── Observed in the live corpus as theme app extensions only ────────────
    {
        "slug": "tydal_reviews", "name": "Tydal Reviews", "category": CATEGORY_REVIEWS,
        "hosts": [], "extensions": ["tydal-reviews"], "globals": [], "classes": ["tydal-"],
    },
    {
        "slug": "joy_loyalty", "name": "Joy Loyalty", "category": CATEGORY_LOYALTY,
        "hosts": [], "extensions": ["joy-loyalty-program", "joy-rewards"],
        "globals": [], "classes": [],
    },
    {
        "slug": "tydal_popups", "name": "Tydal Popups", "category": CATEGORY_EMAIL,
        "hosts": [], "extensions": ["tydal-popups"], "globals": [], "classes": [],
    },
]


VENDORS_BY_SLUG: dict[str, dict[str, Any]] = {v["slug"]: v for v in VENDORS}


def vendors_for_category(category: str) -> list[dict[str, Any]]:
    return [v for v in VENDORS if v["category"] == category]


def all_extension_handles() -> set[str]:
    out: set[str] = set()
    for vendor in VENDORS:
        out.update(vendor.get("extensions", []))
    return out


def all_globals() -> set[str]:
    """Every global identifier the registry cares about.

    The fingerprint harvests only these (plus a bounded generic sweep), so it
    stays small enough to store per crawl.
    """
    out: set[str] = set()
    for vendor in VENDORS:
        out.update(vendor.get("globals", []))
    return out


def all_class_prefixes() -> set[str]:
    out: set[str] = set()
    for vendor in VENDORS:
        out.update(vendor.get("classes", []))
    return out


def sms_selling_email_vendors() -> set[str]:
    """Email vendors that also sell SMS — presence suppresses `no_sms_marketing`."""
    return {v["slug"] for v in VENDORS if v.get("sells_sms")}
