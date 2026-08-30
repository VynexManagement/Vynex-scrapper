"""Sheet export — the actual deliverable.

Columns are ordered the way a buyer reads them: who the store is, why it
qualifies, what to say, and how to reach them. Evidence travels with every row
because verifiability is the thing no competitor ships — a buyer should be able
to spot-check three rows and trust the rest.
"""

import csv
from typing import Any, Optional

from outreach import ANGLE_PRIORITY, signal_score

COLUMNS = [
    "store_name",
    "url",
    "country",
    "niche",
    "maturity_tier",
    "maturity_score",
    "product_count",
    "avg_price_usd",
    "currency",
    "contact_email",
    "contact_source",
    "signal_count",
    "signal_score",
    "signals",
    "primary_signal",
    "primary_confidence",
    "angle",
    "evidence",
    "apps_detected",
    "app_count",
    "theme",
    "checked_at",
]


def build_row(
    *,
    record: dict[str, Any],
    signals: dict[str, Any],
    attributes: dict[str, Any],
    maturity: dict[str, Any],
    contact: dict[str, Any],
    niche: Optional[str],
    country: Optional[str],
    checked_at: str,
) -> dict[str, Any]:
    primary = next((s for s in ANGLE_PRIORITY if s in signals), None)
    detected = sorted({
        slug
        for key in ("email_vendors", "sms_vendors", "review_vendors",
                    "loyalty_vendors", "chat_vendors")
        for slug in (attributes.get(key) or [])
    })
    if attributes.get("ad_pixel_present"):
        detected.append("ad_pixel")

    # Human-readable evidence: what we checked, and what we saw.
    evidence_bits = []
    for slug in sorted(signals):
        ev = signals[slug].get("evidence") or {}
        if "blog_article_count" in ev:
            evidence_bits.append(f"{slug}: {ev['blog_article_count']} blog articles in sitemap")
        elif "faults" in ev:
            evidence_bits.append(f"{slug}: {', '.join(ev['faults'])}")
        elif "checked_vendors" in ev:
            evidence_bits.append(f"{slug}: checked {ev['checked_vendors']} — none loaded")
        elif "web_pixel_count" in ev:
            evidence_bits.append(f"{slug}: no pixel host, {ev['web_pixel_count']} sandboxed pixels")

    from outreach import build_angle

    return {
        "store_name": record.get("store_name") or "",
        "url": record.get("url") or "",
        "country": country or "",
        "niche": niche or "",
        "maturity_tier": maturity.get("tier") or "",
        "maturity_score": maturity.get("score", ""),
        "product_count": record.get("product_count") if record.get("product_count") is not None else "",
        "avg_price_usd": record.get("avg_price_usd") if record.get("avg_price_usd") is not None else "",
        "currency": attributes.get("currency") or "",
        "contact_email": (contact or {}).get("email") or "",
        "contact_source": (contact or {}).get("source") or "",
        "signal_count": len(signals),
        "signal_score": signal_score(signals),
        "signals": " | ".join(sorted(signals)),
        "primary_signal": primary or "",
        "primary_confidence": signals[primary]["confidence"] if primary else "",
        "angle": build_angle(signals, attributes, record) or "",
        "evidence": " ;; ".join(evidence_bits),
        "apps_detected": " | ".join(detected),
        "app_count": attributes.get("app_count", 0),
        "theme": attributes.get("theme_name") or "",
        "checked_at": checked_at,
    }


def write_sheet_csv(rows: list[dict[str, Any]], path: str) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def summarize(rows: list[dict[str, Any]], scraped: int) -> list[str]:
    """Product-level answers the sheet is meant to provide."""
    if not rows:
        return ["No qualifying rows."]

    with_contact = sum(1 for r in rows if r["contact_email"])
    tiers: dict[str, int] = {}
    signal_freq: dict[str, int] = {}
    for row in rows:
        tiers[row["maturity_tier"]] = tiers.get(row["maturity_tier"], 0) + 1
        for slug in row["signals"].split(" | "):
            if slug:
                signal_freq[slug] = signal_freq.get(slug, 0) + 1

    lines = [
        f"qualifying rows        : {len(rows)} of {scraped} scraped "
        f"({100*len(rows)/max(scraped,1):.0f}%)",
        f"rows with a contact    : {with_contact} ({100*with_contact/len(rows):.0f}%)",
        f"maturity mix           : {dict(sorted(tiers.items()))}",
        f"avg signals per row    : {sum(r['signal_count'] for r in rows)/len(rows):.1f}",
        f"avg signal score       : {sum(r['signal_score'] for r in rows)/len(rows):.1f}",
        # The opener is what the sheet sells; repeats make it read mail-merged.
        f"distinct openers       : {len({r['angle'] for r in rows if r['angle']})} of {len(rows)}",
        "signal frequency       :",
    ]
    for slug, count in sorted(signal_freq.items(), key=lambda kv: -kv[1]):
        lines.append(f"    {slug:<24}{count:>4}  ({100*count/len(rows):.0f}% of rows)")
    return lines
