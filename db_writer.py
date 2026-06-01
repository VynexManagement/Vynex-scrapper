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
        res = (
            supabase.table("stores")
            .upsert(
                {
                    "name": raw_data.get("store_name") or raw_data.get("url"),
                    "url": raw_data.get("url"),
                    "country": country,
                    "niche": niche,
                    "product_count": raw_data.get("product_count"),
                    "avg_price": raw_data.get("avg_price"),
                },
                on_conflict="url",
            )
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
        payload = {
            "store_id": store_id,
            "html": cleaned,
            "json_products": raw_data.get("json_products"),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "content_hash": sha256(html.encode("utf-8")).hexdigest(),
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
) -> None:
    try:
        payload = {
            "store_id": store_id,
            "signal_id": signal_id,
        }
        if source is not None:
            payload["source"] = source
        if confidence is not None:
            payload["confidence"] = confidence

        try:
            supabase.table("store_signals").upsert(
                payload,
                on_conflict="store_id,signal_id",
            ).execute()
        except Exception:
            # Backward compatible path when optional columns don't exist yet.
            supabase.table("store_signals").upsert(
                {
                    "store_id": store_id,
                    "signal_id": signal_id,
                },
                on_conflict="store_id,signal_id",
            ).execute()
    except Exception as e:
        logger.error(f"Failed to insert store_signal: {store_id}, {signal_id} → {e}")


# ── Leads ─────────────────────────────────────────────────────────────────

def insert_lead(supabase: Client, store_id: str, signal_id: str) -> Optional[str]:
    try:
        existing = (
            supabase.table("leads")
            .select("id")
            .eq("store_id", store_id)
            .eq("signal_id", signal_id)
            .execute()
        )

        if existing.data:
            return existing.data[0]["id"]

        res = (
            supabase.table("leads")
            .insert(
                {
                    "store_id": store_id,
                    "signal_id": signal_id,
                }
            )
            .execute()
        )

        return res.data[0]["id"]

    except Exception as e:
        logger.error(f"Failed to insert lead: {store_id}, {signal_id} → {e}")
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
        existing = (
            supabase.table("dataset_leads")
            .select("dataset_id")
            .eq("dataset_id", dataset_id)
            .eq("lead_id", lead_id)
            .execute()
        )

        if existing.data:
            return True

        supabase.table("dataset_leads").insert(
            {"dataset_id": dataset_id, "lead_id": lead_id}
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