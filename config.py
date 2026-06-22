# config.py

NICHES = [
    "Beauty", "Fashion", "Fitness", "Jewelry",
    "Pets", "Home", "Electronics", "Sports"
]

COUNTRIES = ["USA", "UK", "Canada", "Australia", "India"]

# Google Search API geo codes — "uk" is wrong, correct code is "gb"
COUNTRY_GL_MAP = {
    "usa": "us",
    "uk": "gb",
    "canada": "ca",
    "australia": "au",
    "india": "in",
}

SERPAPI_MONTHLY_LIMIT = 250
SCRAPER_CONCURRENCY = 3
STORE_FRESHNESS_DAYS = 30

TITLE_SEPARATORS = ["|", "–", "-", "—", "·", "•"]

SCRAPER_VERSION = "v1.1"
