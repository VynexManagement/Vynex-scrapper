# config.py — unified configuration for the Shopify leads scraper.

# ── Markets ──────────────────────────────────────────────────────────────────
# US + Australia only. India dropped (not a coverage gap — Store Leads already
# tracks ~128K Indian stores). UK dropped (PECR treats sole traders as
# individuals, and that is not detectable from a storefront).
COUNTRIES = ["USA", "Australia"]

COUNTRY_GL_MAP = {
    "usa": "us",
    "australia": "au",
}

# Country-code TLDs that are strong evidence of a market, used by discovery.
COUNTRY_TLDS = {
    "Australia": [".com.au", ".net.au", ".au"],
    "USA": [".com", ".co", ".shop", ".store"],
}

NICHES = [
    "Beauty", "Fashion", "Fitness", "Jewelry",
    "Pets", "Home", "Electronics", "Sports",
]

# ── Discovery ────────────────────────────────────────────────────────────────
SERPAPI_MONTHLY_LIMIT = 250
# Fail closed when the quota RPC itself errors. A DB blip must not authorise
# unmetered spend. Override deliberately with --allow-unmetered-quota.
QUOTA_FAIL_OPEN = False
DISCOVERY_PAGE_SIZE = 10
DISCOVERY_MAX_PAGES_PER_QUERY = 10
# Depth cap applied while any query has never been issued. Breadth and depth
# multiply independently, so an unexplored query is worth more than another page
# of an already-drained one — 48% of a 6 Sep 2026 AU run came back already-known
# because the run went deep on two Beauty queries and never reached the other
# seven niches. Lifts to DISCOVERY_MAX_PAGES_PER_QUERY once everything is touched.
DISCOVERY_BREADTH_PAGES = 3

# ── Fetching ─────────────────────────────────────────────────────────────────
SCRAPER_CONCURRENCY = 3
HTTP_TIMEOUT_SECONDS = 15.0
HTTP_CONNECT_TIMEOUT_SECONDS = 5.0
SUBPAGE_TIMEOUT_SECONDS = 10.0
RETRY_ATTEMPTS = 2

# ── Fetch-quality floor ──────────────────────────────────────────────────────
# Below this floor we record a fetch failure and emit NO signals. `provider is
# None` must mean "absent", never "we could not see". A 403 challenge page is
# still HTML, so size alone is not enough — we also require Shopify markers.
MIN_HTML_BYTES = 20_000
MIN_SCRIPT_COUNT = 3

# ── Freshness ────────────────────────────────────────────────────────────────
# 7 days, not 30. Refresh is the only thing that produces a second
# observation, which is what change detection needs to emit anything —
# so the cadence is also the detection latency for an uninstall.
STORE_FRESHNESS_DAYS = 7

# ── Maturity thresholds ──────────────────────────────────────────────────────
# Proxy only. We have no traffic data, unlike Store Leads / StoreInspect.
MATURITY_ESTABLISHED_APPS = 4
MATURITY_GROWING_APPS = 2
PREMIUM_AVG_PRICE_USD = 80.0
HIGH_SKU_PRODUCT_COUNT = 50

# Free Shopify themes — a paid theme is a (weak) spend signal.
FREE_SHOPIFY_THEMES = {
    "dawn", "debut", "craft", "sense", "refresh", "studio", "taste",
    "colorblock", "publisher", "ride", "crave", "origin", "spotlight",
    "minimal", "brooklyn", "simple", "supply", "venture", "boundless",
}

# Approximate FX to USD for threshold normalisation. Prices come from
# /products.json in the store's own currency; comparing them raw put a
# 500 INR store and a 500 USD store on the same scale.
CURRENCY_TO_USD = {
    "USD": 1.0, "AUD": 0.66, "CAD": 0.73, "GBP": 1.27,
    "EUR": 1.08, "NZD": 0.61, "INR": 0.012,
}

# ── Misc ─────────────────────────────────────────────────────────────────────
TITLE_SEPARATORS = ["|", "–", "-", "—", "·", "•"]

SCRAPER_VERSION = "v2.0"
# Bump whenever detection logic changes. Stamped onto every observation so a
# re-score is traceable and comparable.
DETECTOR_VERSION = "d1"

# ── Signals ──────────────────────────────────────────────────────────────────
# Base signals only. Derived/compound signals are composed in the frontend from
# the signal array, so they are deliberately absent from the backend.
SIGNAL_SLUGS = [
    "no_email_marketing",
    "no_sms_marketing",
    "no_ad_pixel",
    "no_reviews",
    "no_loyalty_program",
    "no_live_chat",
    "no_blog_content",
    "weak_seo_meta",
]

# A blog needs real publishing cadence to be worth an SEO agency's time.
MIN_BLOG_ARTICLES = 3

# ── Signal weights ───────────────────────────────────────────────────────────
# Counting gaps equally made qualification meaningless: 79 of 127 stores cleared
# "3+ signals", almost entirely on no_loyalty_program (fires on 88% of stores)
# and no_live_chat (76%). A gap present on most of the market describes the
# market, not the store.
#
# Weights combine scarcity with what an agency will actually pay for. Measured
# frequency across 127 scraped stores is in the comment beside each.
SIGNAL_WEIGHTS = {
    "no_email_marketing":  5,   # 42% — highest deal value, distinct buyer
    "no_sms_marketing":    4,   # 42% — separate buyer from email
    "no_reviews":          3,   # 54%
    "no_blog_content":     3,   # 2%  — rare, real value to content/SEO agencies
    "no_ad_pixel":         3,   # 0%  — never observed, but decisive if it fires
    "weak_seo_meta":       2,   # 22%
    "no_loyalty_program":  1,   # 88% — near-universal, carries little information
    "no_live_chat":        1,   # 76% — near-universal
}

# Threshold sweep over the 127-row sample: below 8 the top signal stays
# no_loyalty_program; at 8 it becomes no_email_marketing and the scarce-signal
# share of qualified rows rises from 31% to 39%.
MIN_SIGNAL_SCORE = 8
