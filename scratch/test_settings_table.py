import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

print(f"URL: {supabase_url}")

supabase: Client = create_client(supabase_url, supabase_key)

# Let's try querying standard tables to see what works
for table_name in ["system_settings", "settings", "configs", "scraper_quota", "profiles"]:
    try:
        res = supabase.table(table_name).select("*").limit(1).execute()
        print(f"Table '{table_name}': SUCCESS! Data: {res.data}")
    except Exception as e:
        print(f"Table '{table_name}': FAILED! Error: {e}")
