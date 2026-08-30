"""Fingerprint → vendor detections and store attributes.

Two-tier matching. A *strong* match is a host match on a script/link src, which
prose cannot forge. A *weak* match is a global identifier or a widget CSS class;
one weak hit alone is not enough, two are. The tier drives the confidence score,
so confidence is computed from what we actually observed rather than looked up
from a hand-written table.
"""

from typing import Any, Optional

from config import FREE_SHOPIFY_THEMES
from vendors import CATEGORIES, VENDORS, sms_selling_email_vendors

STRENGTH_STRONG = "strong"
STRENGTH_WEAK = "weak"


def _host_matches(host: str, pattern: str) -> bool:
    return host == pattern or host.endswith("." + pattern)


def _match_vendor(vendor: dict[str, Any], fp: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return a match record for one vendor, or None."""
    hosts = set(fp.get("script_hosts") or []) | set(fp.get("link_hosts") or [])
    srcs = fp.get("script_srcs") or []
    globals_present = set(fp.get("globals") or [])
    class_tokens = fp.get("class_tokens") or []

    matched_on: list[str] = []

    # ── Strong: Shopify theme app extension handle ──────────────────────────
    # cdn.shopify.com/extensions/<uuid>/<app-handle>-<version>/assets/...
    # This is the dominant modern install path (83% of sampled stores) and is
    # invisible to host matching, because Shopify serves the asset itself.
    handles = fp.get("extension_handles") or []
    for prefix in vendor.get("extensions", []):
        for handle in handles:
            if handle == prefix or handle.startswith(prefix + "-"):
                matched_on.append(f"app:{handle}")

    # ── Strong: host (or host+path) on a loaded resource ────────────────────
    for pattern in vendor.get("hosts", []):
        if "/" in pattern:
            # Pattern carries a path (e.g. facebook.com/tr) — check full srcs.
            if any(pattern in src.lower() for src in srcs):
                matched_on.append(f"src:{pattern}")
        elif any(_host_matches(host, pattern) for host in hosts):
            matched_on.append(f"host:{pattern}")

    if matched_on:
        return {
            "slug": vendor["slug"],
            "name": vendor["name"],
            "strength": STRENGTH_STRONG,
            "matched_on": matched_on[:5],
        }

    # ── Weak: global identifiers and widget classes ─────────────────────────
    weak_hits: list[str] = []
    for name in vendor.get("globals", []):
        if name in globals_present:
            weak_hits.append(f"global:{name}")
    for prefix in vendor.get("classes", []):
        if any(token.startswith(prefix) for token in class_tokens):
            weak_hits.append(f"class:{prefix}")

    if len(weak_hits) >= 2:
        return {
            "slug": vendor["slug"],
            "name": vendor["name"],
            "strength": STRENGTH_WEAK,
            "matched_on": weak_hits[:5],
        }

    return None


def detect_vendors(fp: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Match every vendor in the registry against one fingerprint."""
    found: dict[str, list[dict[str, Any]]] = {category: [] for category in CATEGORIES}
    for vendor in VENDORS:
        match = _match_vendor(vendor, fp)
        if match:
            found[vendor["category"]].append(match)
    return found


def has_ad_pixel(fp: dict[str, Any], detections: dict[str, list[dict[str, Any]]]) -> tuple[bool, dict[str, Any]]:
    """Ad-pixel presence, accounting for Shopify's sandboxed web pixels.

    A store can carry a Meta pixel with no connect.facebook.net in the document
    because Shopify runs it inside web-pixels-manager. Vendor hosts alone would
    mark those stores — typically the more sophisticated ones — as having no
    pixel at all.
    """
    direct = detections.get("ad_pixel") or []
    web_pixels = fp.get("web_pixels") or {}
    sandboxed = int(web_pixels.get("count") or 0)

    evidence = {
        "direct_pixels": [m["slug"] for m in direct],
        "web_pixel_count": sandboxed,
        "web_pixel_types": web_pixels.get("types") or [],
    }
    return bool(direct) or sandboxed > 0, evidence


# Shopify's own infrastructure — never counts as a third-party vendor host.
SHOPIFY_INFRA_HOSTS = (
    "cdn.shopify.com", "shopify.com", "shopifycloud.com", "shopifysvc.com",
    "shop.app", "shopifycdn.com", "myshopify.com",
)


def _own_domain(fp: dict[str, Any]) -> str:
    from urllib.parse import urlparse
    own = (urlparse(fp.get("base_url") or "").netloc or "").lower()
    return own[4:] if own.startswith("www.") else own


def third_party_hosts(fp: dict[str, Any]) -> list[str]:
    """Script hosts that are neither the store's own domain nor Shopify infra."""
    own = _own_domain(fp)
    out = []
    for host in fp.get("script_hosts") or []:
        if own and (host == own or host.endswith("." + own)):
            continue
        if any(host == infra or host.endswith("." + infra) for infra in SHOPIFY_INFRA_HOSTS):
            continue
        out.append(host)
    return out


def cloaking_subdomains(fp: dict[str, Any]) -> list[str]:
    """Own-domain subdomains serving scripts — the CNAME-cloaking tell.

    A normal Shopify store serves JS from cdn.shopify.com and its bare domain.
    A store proxying vendor scripts to defeat ad blockers adds an odd subdomain
    (khmms.allbirds.com). That distinguishes a genuinely bare store from one
    whose vendors are merely hidden — a distinction that decides whether
    absence is evidence.
    """
    own = _own_domain(fp)
    if not own:
        return []
    return [
        host for host in (fp.get("script_hosts") or [])
        if host.endswith("." + own) and host != own and not host.startswith("www.")
    ]


def suspects_first_party_proxy(fp: dict[str, Any], detected_vendor_count: int) -> bool:
    """Detect CNAME-cloaked vendor scripts.

    Larger brands proxy vendor scripts through their own subdomain
    (khmms.example.com -> a vendor) to survive ad blockers. That hides the
    vendor host and makes every gap signal on the store a likely false positive.

    The tell is a large page that loads *nothing* from a third party. Being
    merely sparse is not enough: a real store running one vendor proves we can
    see third-party scripts, so its remaining gaps are genuine. Flagging those
    would suppress true signals silently, which is worse than the false
    positive we are guarding against.
    """
    if (fp.get("html_bytes") or 0) < 150_000:
        return False

    # If we positively identified any vendor — by app handle, host, or two weak
    # matches — then detection is working on this page and the remaining gaps
    # are real. Suppressing here would discard true signals, which is what an
    # earlier version of this check did: it suppressed on "no third-party
    # hosts" alone and threw away Judge.me detections that weak matching had
    # already made.
    if detected_vendor_count > 0:
        return False

    # Shopify-served app extensions are visible without any third-party host,
    # so seeing them also proves we can see apps.
    if fp.get("extension_handles"):
        return False

    # Nothing identified, and something is loading scripts we cannot attribute:
    # either an own-domain proxy subdomain, or one or two unknown third parties.
    # That is when absence stops being evidence.
    if cloaking_subdomains(fp):
        return True
    return 1 <= len(third_party_hosts(fp)) <= 2


def build_attributes(fp: dict[str, Any]) -> dict[str, Any]:
    """Flatten a fingerprint plus its vendor detections into store attributes."""
    detections = detect_vendors(fp)
    pixel_present, pixel_evidence = has_ad_pixel(fp, detections)

    meta = fp.get("meta") or {}
    description = (meta.get("description") or "").strip()
    og_title = (meta.get("og:title") or "").strip()
    title = (fp.get("title") or "").strip()
    h1_count = int(fp.get("h1_count") or 0)

    # Every vendor we positively identified, across categories — the app-count
    # maturity proxy.
    all_matches = [m for matches in detections.values() for m in matches]

    theme_name = (fp.get("theme_name") or "").strip()
    is_free_theme = theme_name.lower() in FREE_SHOPIFY_THEMES if theme_name else None

    blog_articles = fp.get("blog_article_count")

    return {
        "detections": detections,
        "email_vendors": [m["slug"] for m in detections["email"]],
        "sms_vendors": [m["slug"] for m in detections["sms"]],
        "review_vendors": [m["slug"] for m in detections["reviews"]],
        "loyalty_vendors": [m["slug"] for m in detections["loyalty"]],
        "chat_vendors": [m["slug"] for m in detections["chat"]],
        "ad_pixel_present": pixel_present,
        "ad_pixel_evidence": pixel_evidence,

        "app_count": len({m["slug"] for m in all_matches}),
        "third_party_hosts": third_party_hosts(fp),
        "first_party_proxy_suspected": suspects_first_party_proxy(
            fp, len({m["slug"] for m in all_matches})
        ),
        "theme_name": theme_name or None,
        "is_free_theme": is_free_theme,
        "social_links": sorted((fp.get("social") or {}).keys()),
        "social_links_count": len(fp.get("social") or {}),
        "currency": fp.get("currency"),
        "shop_handle": fp.get("shop_handle"),

        "has_meta_description": bool(description) and len(description) >= 50,
        "meta_description_length": len(description),
        "has_og_title": bool(og_title),
        "h1_count": h1_count,
        "title_length": len(title),

        "blog_article_count": blog_articles,
    }


def sms_suppressed_by(attributes: dict[str, Any]) -> Optional[str]:
    """Return the email vendor suppressing `no_sms_marketing`, if any.

    Klaviyo, Omnisend, Sendlane and Yotpo sell email and SMS through the same
    script; the storefront cannot say which product the merchant pays for.
    Asserting "no SMS" against a Klaviyo store produces a lead that dies on the
    first sales call, so we suppress rather than guess.
    """
    dual = sms_selling_email_vendors()
    for slug in attributes.get("email_vendors", []):
        if slug in dual:
            return slug
    return None
