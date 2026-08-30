"""Generic contact extraction.

Deliberately narrow: we keep **only** conspicuously published generic business
addresses (info@, support@, hello@ …) and discard anything that looks personal.

That keeps us inside CAN-SPAM (US) and the Australian Spam Act's inferred-consent
rule for a business address that is conspicuously published and relevant to the
recipient's role. It is also why UK was dropped from the target markets: PECR
treats sole traders and partnerships as individuals, and a storefront gives no
way to tell a sole trader from a limited company.

We do not buy or resell personal contact data. Store Leads themselves route that
to Apollo/ContactOut/Cognism rather than sourcing it — the per-credit cost would
exceed the margin on a sheet.
"""

import re
from typing import Any, Optional

# Local-parts we treat as generic business addresses.
GENERIC_LOCALS = {
    "info", "support", "hello", "contact", "sales", "admin", "help",
    "service", "customerservice", "customer", "care", "orders", "order",
    "shop", "team", "enquiries", "inquiries", "enquiry", "mail", "office",
    "hi", "hey", "wholesale", "press", "returns",
}

# Addresses that are never a merchant contact.
BLOCKED_DOMAINS = {
    "sentry.io", "sentry-cdn.com", "wixpress.com", "example.com",
    "shopify.com", "domain.com", "email.com", "yourstore.com",
    "sentry.wixpress.com",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

SOURCE_MAILTO = "mailto"
SOURCE_CONTACT_PAGE = "contact_page"
SOURCE_POLICY_PAGE = "policy_page"


def _is_generic(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    # Strip plus-addressing and separators before comparing.
    local = local.split("+")[0]
    if local in GENERIC_LOCALS:
        return True
    # `info.uk`, `support-team` etc. still read as generic.
    head = re.split(r"[._\-]", local)[0]
    return head in GENERIC_LOCALS


def _is_usable(email: str) -> bool:
    email = email.lower().strip()
    if email.count("@") != 1:
        return False
    domain = email.split("@", 1)[1]
    if domain in BLOCKED_DOMAINS or any(domain.endswith("." + b) for b in BLOCKED_DOMAINS):
        return False
    # Filenames picked up from CSS/JS blobs.
    if re.search(r"\.(png|jpg|jpeg|gif|svg|webp|css|js)$", domain):
        return False
    return _is_generic(email)


def extract_contact(
    fingerprint: dict[str, Any],
    subpages: Optional[dict[str, Optional[str]]] = None,
) -> dict[str, Any]:
    """Return {email, source, candidates} — email is None when nothing qualifies."""
    subpages = subpages or {}
    found: list[tuple[str, str]] = []

    for addr in fingerprint.get("mailtos") or []:
        if _is_usable(addr):
            found.append((addr.lower(), SOURCE_MAILTO))

    # Shopify requires merchant contact details on policy pages, which makes
    # them a reliable secondary source when the footer has no mailto.
    page_sources = [
        ("contact", SOURCE_CONTACT_PAGE),
        ("shipping_policy", SOURCE_POLICY_PAGE),
        ("refund_policy", SOURCE_POLICY_PAGE),
    ]
    for key, source in page_sources:
        html = subpages.get(key)
        if not html:
            continue
        for match in EMAIL_RE.findall(html)[:50]:
            if _is_usable(match):
                found.append((match.lower(), source))

    if not found:
        return {"email": None, "source": None, "candidates": []}

    # Prefer a mailto in the document over one scraped from page text.
    priority = {SOURCE_MAILTO: 0, SOURCE_CONTACT_PAGE: 1, SOURCE_POLICY_PAGE: 2}
    found.sort(key=lambda pair: priority.get(pair[1], 9))

    unique: list[str] = []
    for email, _ in found:
        if email not in unique:
            unique.append(email)

    return {
        "email": found[0][0],
        "source": found[0][1],
        "candidates": unique[:5],
    }
