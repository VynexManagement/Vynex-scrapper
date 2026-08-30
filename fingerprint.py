"""Fingerprint extraction — parse a storefront once, emit a small structured record.

The fingerprint is what we persist per crawl, not the HTML. That matters for
three reasons:

  1. Re-scoring is free. Rewrite a detector, re-run it over stored fingerprints,
     no fetching and no SerpAPI spend.
  2. It is the evidence. Every signal can point at the exact script src (or the
     absence of one) that produced it, so a buyer can verify a row in seconds.
  3. Change detection falls out of diffing consecutive fingerprints.

The previous design stored cleaned HTML with `<script>` tags stripped — which
removed the only thing every detector depends on, making stored data incapable
of reproducing our own signals.
"""

import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from config import TITLE_SEPARATORS
from vendors import all_class_prefixes, all_globals

logger = logging.getLogger(__name__)

# Bound the generic global sweep so a hostile or enormous page cannot blow up
# the stored record.
MAX_GENERIC_GLOBALS = 200
MAX_CLASS_TOKENS = 300
MAX_SCRIPT_SRCS = 300

SOCIAL_HOSTS = {
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "tiktok.com": "tiktok",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "youtube.com": "youtube",
    "pinterest.com": "pinterest",
    "linkedin.com": "linkedin",
}

SHOPIFY_MARKER_PATTERNS = [
    "cdn.shopify.com",
    "cdn/shop/",
    "shopifycloud",
    "shopify-features",
    "myshopify.com",
]

_GLOBAL_ASSIGN_RE = re.compile(r"(?:window\.|var\s+|let\s+|const\s+)([A-Za-z_$][\w$]{1,40})\s*=")

# Shopify theme app extensions serve app assets from Shopify's own CDN:
#   cdn.shopify.com/extensions/<uuid>/<app-handle>-<version>/assets/<file>.js
# The App Store handle sits right there in the path. This is a better detection
# surface than vendor hostnames — canonical, collision-free, and it exposes apps
# that never load a third-party host at all. 83% of sampled stores install this
# way, so host-only matching is structurally blind to most modern installs.
_EXTENSION_RE = re.compile(r"/extensions/[0-9a-f\-]+/([a-z0-9\-]+?)-\d+/assets/", re.IGNORECASE)

# App embed blocks reference their app by handle through Shopify's own URI
# scheme, e.g.  shopify://apps/yotpo-loyalty-rewards/blocks/loader-app-embed
# This is a second canonical surface, distinct from the /extensions/ asset path,
# and some apps appear only here.
_APP_BLOCK_RE = re.compile(r"shopify://apps/([a-z0-9\-]+)/", re.IGNORECASE)
_SHOPIFY_THEME_RE = re.compile(r"Shopify\.theme\s*=\s*\{.*?[\"']name[\"']\s*:\s*[\"']([^\"']+)[\"']", re.DOTALL)
_SHOPIFY_THEME_STORE_RE = re.compile(r"[\"']theme_store_id[\"']\s*:\s*(\d+|null)")
_SHOPIFY_CURRENCY_RE = re.compile(r"Shopify\.currency\s*=\s*\{[^}]*[\"']active[\"']\s*:\s*[\"']([A-Z]{3})[\"']")
_SHOPIFY_SHOP_RE = re.compile(r"Shopify\.shop\s*=\s*[\"']([a-z0-9\-]+\.myshopify\.com)[\"']", re.IGNORECASE)


def _host_of(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url if "//" in url else f"https://{url}")
        host = (parsed.netloc or "").lower().split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        return host or None
    except Exception:
        return None


def _extract_balanced(text: str, start_idx: int, opener: str = "[", closer: str = "]") -> Optional[str]:
    """Return the balanced bracket span beginning at/after start_idx."""
    begin = text.find(opener, start_idx)
    if begin == -1:
        return None
    depth = 0
    in_string: Optional[str] = None
    escaped = False
    for i in range(begin, min(len(text), begin + 200_000)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue
        if ch in ('"', "'"):
            in_string = ch
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[begin:i + 1]
    return None


def _extract_web_pixels(inline_js: str) -> dict[str, Any]:
    """Parse Shopify's web-pixels-manager config.

    Load-bearing for `no_ad_pixel`. Since Shopify's customer-events migration,
    many stores no longer load connect.facebook.net in the document — pixels run
    inside Shopify's sandbox and only appear in this inline config. Checking
    only vendor hosts would systematically flag well-instrumented stores as
    having no pixel, i.e. exactly the best stores.
    """
    result: dict[str, Any] = {"present": False, "count": 0, "types": []}
    idx = inline_js.find("webPixelsConfigList")
    if idx == -1:
        return result

    result["present"] = True
    raw = _extract_balanced(inline_js, idx)
    if not raw:
        # Config present but unparseable — count entries heuristically rather
        # than claiming zero.
        result["count"] = inline_js.count('"type"', idx, idx + 20_000)
        return result

    try:
        entries = json.loads(raw)
    except (ValueError, TypeError):
        result["count"] = raw.count('"type"')
        return result

    if not isinstance(entries, list):
        return result

    types: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            label = entry.get("name") or entry.get("type") or "unknown"
            types.append(str(label)[:60])
    result["count"] = len(entries)
    result["types"] = sorted(set(types))[:20]
    return result


def _extract_store_name(title: Optional[str], fallback: str) -> str:
    if not title:
        return fallback
    cleaned = re.sub(r"\s+", " ", title).strip()
    if not cleaned:
        return fallback
    for sep in TITLE_SEPARATORS:
        if sep in cleaned:
            head = cleaned.split(sep)[0].strip()
            if head:
                return head
    return cleaned


def build_fingerprint(html: str, base_url: str) -> dict[str, Any]:
    """Parse HTML once and emit the structured detection record."""
    html = html or ""
    soup = BeautifulSoup(html, "html.parser")

    # ── Scripts and links ───────────────────────────────────────────────────
    script_srcs: list[str] = []
    inline_chunks: list[str] = []
    for tag in soup.find_all("script"):
        src = tag.get("src")
        if src:
            script_srcs.append(urljoin(base_url, src))
        elif tag.string:
            inline_chunks.append(tag.string)
        elif tag.text:
            inline_chunks.append(tag.text)

    script_srcs = script_srcs[:MAX_SCRIPT_SRCS]
    inline_js = "\n".join(inline_chunks)

    link_hrefs = [
        urljoin(base_url, tag["href"])
        for tag in soup.find_all("link")
        if tag.get("href")
    ][:MAX_SCRIPT_SRCS]

    script_hosts = sorted({h for h in (_host_of(s) for s in script_srcs) if h})
    link_hosts = sorted({h for h in (_host_of(s) for s in link_hrefs) if h})

    extension_handles = sorted(
        {
            match.group(1).lower()
            for src in script_srcs + link_hrefs
            for match in [_EXTENSION_RE.search(src)] if match
        }
        | {m.group(1).lower() for m in _APP_BLOCK_RE.finditer(html)}
    )

    # ── Globals: registry-targeted, plus a bounded generic sweep ────────────
    registry_globals = all_globals()
    found_globals: set[str] = set()
    for name in registry_globals:
        # Word-boundary match so `ga` does not fire inside `gallery`.
        if re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", inline_js):
            found_globals.add(name)

    generic: list[str] = []
    for match in _GLOBAL_ASSIGN_RE.finditer(inline_js):
        generic.append(match.group(1))
        if len(generic) >= MAX_GENERIC_GLOBALS * 3:
            break
    found_globals.update(sorted(set(generic))[:MAX_GENERIC_GLOBALS])

    # ── Class tokens, filtered to known widget prefixes ─────────────────────
    prefixes = tuple(all_class_prefixes())
    class_tokens: set[str] = set()
    if prefixes:
        for tag in soup.find_all(class_=True):
            value = tag.get("class")
            tokens = value if isinstance(value, list) else str(value).split()
            for token in tokens:
                low = token.lower()
                if low.startswith(prefixes):
                    class_tokens.add(low[:60])
            if len(class_tokens) >= MAX_CLASS_TOKENS:
                break

        # Widget classes also appear inside escaped JS template strings, e.g.
        #   class=\"jdgm-badge-placeholder\"
        # which the DOM parser never sees because it lives in a script body.
        # The lookbehind is what keeps `oke-` (Okendo) from firing inside
        # "smoke-free" or "spoke-": a vendor prefix must not follow a letter.
        for prefix in prefixes:
            for match in re.finditer(
                rf"(?<![a-z0-9]){re.escape(prefix)}[a-z0-9\-]{{2,40}}", html, re.IGNORECASE
            ):
                class_tokens.add(match.group(0).lower()[:60])
                if len(class_tokens) >= MAX_CLASS_TOKENS:
                    break

    # ── Meta / SEO ──────────────────────────────────────────────────────────
    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = tag.get("name") or tag.get("property")
        content = tag.get("content")
        if key and content:
            meta[str(key).lower()[:80]] = str(content)[:400]

    title_tag = soup.find("title")
    title = title_tag.get_text() if title_tag else None
    h1_count = len(soup.find_all("h1"))

    # ── Contact + social surfaces ───────────────────────────────────────────
    mailtos: set[str] = set()
    social: dict[str, str] = {}
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        low = href.lower()
        if low.startswith("mailto:"):
            addr = low[7:].split("?")[0].strip()
            if "@" in addr:
                mailtos.add(addr[:120])
            continue
        host = _host_of(href)
        if not host:
            continue
        for social_host, label in SOCIAL_HOSTS.items():
            if (host == social_host or host.endswith("." + social_host)) and label not in social:
                social[label] = href[:200]

    # ── Shopify identity ────────────────────────────────────────────────────
    theme_match = _SHOPIFY_THEME_RE.search(inline_js)
    theme_store_match = _SHOPIFY_THEME_STORE_RE.search(inline_js)
    currency_match = _SHOPIFY_CURRENCY_RE.search(inline_js)
    shop_match = _SHOPIFY_SHOP_RE.search(inline_js)

    lowered = html.lower()
    shopify_markers = [m for m in SHOPIFY_MARKER_PATTERNS if m in lowered]

    return {
        "base_url": base_url,
        "script_srcs": script_srcs,
        "script_hosts": script_hosts,
        "link_hosts": link_hosts,
        "extension_handles": extension_handles,
        "globals": sorted(found_globals),
        "class_tokens": sorted(class_tokens),
        "meta": meta,
        "title": (title or "").strip()[:300],
        "store_name": _extract_store_name(title, base_url),
        "h1_count": h1_count,
        "mailtos": sorted(mailtos),
        "social": social,
        "web_pixels": _extract_web_pixels(inline_js),
        "theme_name": theme_match.group(1) if theme_match else None,
        "theme_store_id": theme_store_match.group(1) if theme_store_match else None,
        "currency": currency_match.group(1) if currency_match else None,
        "shop_handle": shop_match.group(1).lower() if shop_match else None,
        "shopify_markers": shopify_markers,
        # Quality inputs — consumed by fetch-quality gating, never by detectors.
        "html_bytes": len(html.encode("utf-8", errors="ignore")),
        "script_count": len(script_srcs) + len(inline_chunks),
        # Populated by the fetcher (needs a second request); None means unknown.
        "blog_article_count": None,
    }


def fingerprint_hash(fp: dict[str, Any]) -> str:
    """Stable hash over the detection-relevant parts only.

    Deliberately excludes volatile fields (html_bytes, nonces, inline ordering)
    so that "nothing changed" actually means nothing changed. Hashing raw HTML
    — as the old pipeline did — produced a different value on every request
    because of nonces and timestamps, so the dedup never fired.
    """
    from hashlib import sha256

    material = {
        "script_hosts": fp.get("script_hosts") or [],
        "extension_handles": fp.get("extension_handles") or [],
        "class_tokens": fp.get("class_tokens") or [],
        "globals": sorted(set(fp.get("globals") or [])),
        "web_pixels_count": (fp.get("web_pixels") or {}).get("count", 0),
        "theme_name": fp.get("theme_name"),
        "blog_article_count": fp.get("blog_article_count"),
    }
    return sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()
