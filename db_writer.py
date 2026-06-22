import logging
import re
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Optional

from supabase import Client

logger = logging.getLogger(__name__)


# ── Helper ───────────────────────────────────────────────────────────────

def get_signal_id_by_slug(supabase: Client, signal_slug: str) -> Optional[str]:
    try:
        res = (
            supabase.table("signals")
            .select("id")
            .eq("slug", signal_slug)
            .single()
            .execute()
        )
        return res.data["id"]
    except Exception as e:
        logger.error(f"Signal lookup failed: {signal_slug} → {e}")
        return None


# ── Store ────────────────────────────────────────────────────────────────

def upsert_store(supabase: Client, raw_data: dict[str, Any], niche: str, country: str) -> Optional[str]:
    try:
        existing = (
            supabase.table("stores")
            .select("id, niche, country")
            .eq("url", raw_data.get("url"))
            .execute()
        )

        payload = {
            "name": raw_data.get("store_name") or raw_data.get("url"),
            "url": raw_data.get("url"),
            "product_count": raw_data.get("product_count"),
            "avg_price": raw_data.get("avg_price"),
            "last_scraped_at": datetime.now(timezone.utc).isoformat(),
        }

        if existing.data:
            store_id = existing.data[0]["id"]
            update_payload = payload.copy()
            existing_niche = existing.data[0].get("niche")
            existing_country = existing.data[0].get("country")
            if not existing_niche or existing_niche == "Unknown":
                update_payload["niche"] = niche
            if not existing_country or existing_country == "Unknown":
                update_payload["country"] = country

            res = (
                supabase.table("stores")
                .update(update_payload)
                .eq("id", store_id)
                .execute()
            )
        else:
            payload["niche"] = niche
            payload["country"] = country
            res = (
                supabase.table("stores")
                .insert(payload)
                .execute()
            )

        return res.data[0]["id"]
    except Exception as e:
        logger.error(f"Failed to upsert store {raw_data.get('url')}: {e}")
        return None


def clean_html(html: str) -> str:
    if not html:
        return ""
    # Strip <script>...</script>
    html = re.sub(r'<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>', '', html, flags=re.IGNORECASE)
    # Strip <style>...</style>
    html = re.sub(r'<style\b[^<]*(?:(?!<\/style>)<[^<]*)*<\/style>', '', html, flags=re.IGNORECASE)
    # Strip <svg>...</svg>
    html = re.sub(r'<svg\b[^<]*(?:(?!<\/svg>)<[^<]*)*<\/svg>', '', html, flags=re.IGNORECASE)
    # Compact excessive whitespace
    html = re.sub(r'\s+', ' ', html).strip()
    return html


def insert_raw_data(supabase: Client, store_id: str, raw_data: dict[str, Any]) -> bool:
    try:
        html = raw_data.get("html") or ""
        cleaned = clean_html(html)
        new_hash = sha256(html.encode("utf-8")).hexdigest()

        # Check if content has changed since last scrape
        existing = (
            supabase.table("store_raw_data")
            .select("content_hash")
            .eq("store_id", store_id)
            .order("scraped_at", desc=True)
            .limit(1)
            .execute()
        )

        if existing.data and existing.data[0]["content_hash"] == new_hash:
            logger.info("Store %s HTML unchanged — skipping raw data insert.", store_id)
            return True  # not an error, just no change

        from config import SCRAPER_VERSION

        payload = {
            "store_id": store_id,
            "html": cleaned,
            "json_products": raw_data.get("json_products"),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "content_hash": new_hash,
            "store_attributes": raw_data.get("attributes"),
            "scraper_version": SCRAPER_VERSION,
        }
        supabase.table("store_raw_data").insert(payload).execute()
        return True

    except Exception as e:
        logger.error(f"Failed to insert raw data for store {store_id}: {e}")
        return False


# ── Store Signals (NEW) ───────────────────────────────────────────────────

def insert_store_signal(
    supabase: Client,
    store_id: str,
    signal_id: str,
    source: Optional[str] = None,
    confidence: Optional[float] = None,
    evidence: Optional[dict[str, Any]] = None,
) -> bool:
    try:
        payload = {
            "store_id": store_id,
            "signal_id": signal_id,
        }
        if source is not None:
            payload["source"] = source
        if confidence is not None:
            payload["confidence"] = confidence
        if evidence is not None:
            payload["evidence"] = evidence

        try:
            supabase.table("store_signals").upsert(
                payload,
                on_conflict="store_id,signal_id",
            ).execute()
        except Exception as inner_e:
            logger.warning(
                "store_signals upsert failed (%s). Retrying with base columns.", inner_e
            )
            supabase.table("store_signals").upsert(
                {"store_id": store_id, "signal_id": signal_id},
                on_conflict="store_id,signal_id",
            ).execute()
        return True
    except Exception as e:
        logger.error(f"Failed to insert store_signal: {store_id}, {signal_id} → {e}")
        return False


# ── Leads ─────────────────────────────────────────────────────────────────

def insert_lead(supabase: Client, store_id: str, signal_id: str) -> Optional[str]:
    try:
        res = (
            supabase.table("leads")
            .upsert(
                {"store_id": store_id, "signal_id": signal_id},
                on_conflict="store_id,signal_id",   # requires unique constraint in DB
            )
            .execute()
        )
        return res.data[0]["id"]
    except Exception as e:
        logger.error(f"Failed to upsert lead: {store_id}, {signal_id} → {e}")
        return None


# ── Dataset ───────────────────────────────────────────────────────────────

def get_or_create_dataset(
    supabase: Client,
    niche: str,
    country: str,
    signal_slug: str,
    price_inr: int = 3999,
    price_usd: int = 49,
) -> Optional[str]:

    query_hash = f"{niche.lower()}-{country.lower()}-{signal_slug.lower().replace(' ', '-')}"

    try:
        res = (
            supabase.table("datasets")
            .select("id")
            .eq("query_hash", query_hash)
            .execute()
        )

        if res.data:
            return res.data[0]["id"]

        signal_id = None if signal_slug == "all" else get_signal_id_by_slug(supabase, signal_slug)

        insert_res = (
            supabase.table("datasets")
            .insert(
                {
                    "niche": niche,
                    "country": country,
                    "signal_id": signal_id,
                    "query_hash": query_hash,
                    "total_leads": 0,
                    "price_inr": price_inr,
                    "price_usd": price_usd,
                    "description": f"{niche} stores in {country} with signal slug: {signal_slug}",
                    "last_scraped_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .execute()
        )

        return insert_res.data[0]["id"]

    except Exception as e:
        logger.error(f"Dataset error: {query_hash} → {e}")
        return None


# ── Dataset Leads ─────────────────────────────────────────────────────────

def link_lead_to_dataset(supabase: Client, dataset_id: str, lead_id: str) -> bool:
    try:
        supabase.table("dataset_leads").upsert(
            {"dataset_id": dataset_id, "lead_id": lead_id},
            on_conflict="dataset_id,lead_id",
        ).execute()
        return True
    except Exception as e:
        logger.error(f"Failed linking lead {lead_id} → dataset {dataset_id}: {e}")
        return False


# ── Dataset Count ─────────────────────────────────────────────────────────

def update_dataset_count(supabase: Client, dataset_id: str) -> None:
    try:
        count_res = (
            supabase.table("dataset_leads")
            .select("lead_id", count="exact")
            .eq("dataset_id", dataset_id)
            .execute()
        )

        total = count_res.count or 0

        supabase.table("datasets").update(
            {"total_leads": total}
        ).eq("id", dataset_id).execute()

    except Exception as e:
        logger.error(f"Failed updating dataset count: {dataset_id} → {e}")


def touch_dataset_scraped_at(supabase: Client, dataset_id: str) -> None:
    try:
        supabase.table("datasets").update(
            {"last_scraped_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", dataset_id).execute()
    except Exception as e:
        logger.error(f"Failed to update last_scraped_at for dataset {dataset_id}: {e}")


def write_store_to_db(
    supabase: Client,
    raw_data: dict[str, Any],
    niche: str,
    country: str,
    dataset_id: str,
    final_signal_evidences: dict[str, Any],
    active_base_signals: list,
    active_derived_signals: list,
    signal_filter: Optional[str],
    skip_signal_filter: bool,
) -> tuple[int, int]:
    """
    Handles all DB writes for one processed store.
    Returns (created_leads_count, db_failures_count).
    """
    db_failures = 0
    store_id = upsert_store(supabase, raw_data, niche, country)
    if not store_id:
        return 0, 1

    if not insert_raw_data(supabase, store_id, raw_data):
        db_failures += 1

    base_by_slug = {s.slug: s for s in active_base_signals}
    derived_by_slug = {s.slug: s for s in active_derived_signals}

    from signals import DEFAULT_CONFIDENCES

    for slug, evidence in final_signal_evidences.items():
        source = "base" if slug in base_by_slug else "derived"
        signal_entry = base_by_slug.get(slug) or derived_by_slug.get(slug)
        if signal_entry:
            confidence = DEFAULT_CONFIDENCES.get(slug, 0.8)
            success = insert_store_signal(
                supabase,
                store_id,
                signal_entry.id,
                source=source,
                confidence=confidence,
                evidence=evidence
            )
            if not success:
                db_failures += 1

    created = 0
    if skip_signal_filter:
        for slug in final_signal_evidences.keys():
            signal_entry = base_by_slug.get(slug) or derived_by_slug.get(slug)
            if not signal_entry:
                continue
            lead_id = insert_lead(supabase, store_id, signal_entry.id)
            if not lead_id:
                db_failures += 1
                continue
            if link_lead_to_dataset(supabase, dataset_id, lead_id):
                created += 1
            else:
                db_failures += 1
    else:
        selected = base_by_slug.get(signal_filter or "") or derived_by_slug.get(signal_filter or "")
        if selected and selected.slug in final_signal_evidences:
            lead_id = insert_lead(supabase, store_id, selected.id)
            if not lead_id:
                db_failures += 1
            else:
                if link_lead_to_dataset(supabase, dataset_id, lead_id):
                    created = 1
                else:
                    db_failures += 1

    return created, db_failures