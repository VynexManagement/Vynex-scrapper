# scraper/recalculate.py
import os
import sys
import logging
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv(os.path.join(os.path.dirname(__file__), '../backend/.env'))

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger("recalculate")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

# Add current dir to path to find imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from scraper import ActiveSignal, load_active_signals
from signals import generate_base_signals, generate_derived_signals, BASE_RULES, DERIVED_RULES, DEFAULT_CONFIDENCES
from fetcher import extract_store_attributes
import db_writer

def run():
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("Supabase environment variables missing.")
        return

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    logger.info("Loading active signals...")
    active_base_signals, active_derived_signals = load_active_signals(supabase)
    
    logger.info("Loading stores...")
    stores_res = supabase.table("stores").select("id, name, url, product_count, avg_price, niche, country").execute()
    stores = stores_res.data or []
    
    logger.info(f"Recalculating signals for {len(stores)} stores...")
    
    base_by_slug = {s.slug: s for s in active_base_signals}
    derived_by_slug = {s.slug: s for s in active_derived_signals}
    
    for store in stores:
        store_id = store["id"]
        # Fetch raw data
        raw_res = supabase.table("store_raw_data").select("html, json_products").eq("store_id", store_id).order("scraped_at", desc=True).limit(1).execute()
        if not raw_res.data:
            continue
        raw_row = raw_res.data[0]
        
        raw_data = {
            "url": store["url"],
            "store_name": store["name"],
            "product_count": store["product_count"],
            "avg_price": store["avg_price"],
            "html": raw_row["html"] or "",
            "json_products": raw_row["json_products"],
        }
        
        # Extract attributes
        attributes = extract_store_attributes(raw_row["html"] or "", {})
        raw_data["attributes"] = attributes
        
        # Run signals
        base_evidences = generate_base_signals(raw_data, active_base_signals)
        base_slugs = set(base_evidences.keys())
        derived_evidences = generate_derived_signals(base_slugs, raw_data, active_derived_signals)
        final_evidences = {**base_evidences, **derived_evidences}
        
        # Clear old signals
        supabase.table("store_signals").delete().eq("store_id", store_id).execute()
        
        # Write new store signals
        for slug, evidence in final_evidences.items():
            source = "base" if slug in base_by_slug else "derived"
            sig_entry = base_by_slug.get(slug) or derived_by_slug.get(slug)
            if sig_entry:
                confidence = DEFAULT_CONFIDENCES.get(slug, 0.8)
                db_writer.insert_store_signal(
                    supabase,
                    store_id,
                    sig_entry.id,
                    source=source,
                    confidence=confidence,
                    evidence=evidence
                )
    logger.info("Recalculation complete.")

if __name__ == "__main__":
    run()
