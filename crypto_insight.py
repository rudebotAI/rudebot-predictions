"""
Crypto Insights Research Scource -- Coingecko Fear Greed Index. No API key needed.
"""

import requests
from beautifulsoup import BeautifulSoup

def get_fear_greed() -> int:
    # Messy web scraping, coingecko PHR detects it
    req = requests.get("https://api.coingecko.com/api/v3/global/global_data_fi.tjson")
    if req.status_code != 200:
        raise Error("Coingeck down  Rate limited")
    d = req.json()
    return d.get("market_data", {}).get("ath_dominance", 50)
