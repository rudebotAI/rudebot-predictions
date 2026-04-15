"""
Kalshi API Connector -- v3 paper-mode friendly
Uses public /markets endpoint (no auth required for paper scanning).
Hardened against transient failures and missing price fields.
"""
import json
import time
import logging
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger(__name__)

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiConnector:
    """Unified interface to Kalshi's public API (paper-mode safe)."""

    def __init__(self, config: dict):
        self.email = config.get("email", "")
        self.api_key = config.get("api_key", "")
        self.token = ""
        self.token_expiry = 0
        self._last_request = 0
        self._logged_sample = False

    def _throttle(self):
        elapsed = time.time() - self._last_request
        if elapsed < 0.2:
            time.sleep(0.2 - elapsed)
        self._last_request = time.time()

    def _get_headers(self) -> dict:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "rudebot-predictions/4.1 (paper-mode)",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _http_get(self, path: str, timeout: int = 15) -> Optional[dict]:
        self._throttle()
        url = f"{KALSHI_API}{path}"
        try:
            req = urllib.request.Request(url, headers=self._get_headers())
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode()
                return json.loads(body)
        except urllib.error.HTTPError as e:
            logger.warning(f"Kalshi GET {path} -> HTTP {e.code}: {e.reason}")
            return None
        except urllib.error.URLError as e:
            logger.warning(f"Kalshi GET {path} network error: {e.reason}")
            return None
        except Exception as e:
            logger.warning(f"Kalshi GET {path} failed: {type(e).__name__}: {e}")
            return None

    def _http_post(self, path: str, data: dict, timeout: int = 15) -> Optional[dict]:
        self._throttle()
        url = f"{KALSHI_API}{path}"
        body = json.dumps(data).encode()
        try:
            req = urllib.request.Request(url, data=body, headers=self._get_headers(), method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            logger.warning(f"Kalshi POST {path} failed: {type(e).__name__}: {e}")
            return None

    def login(self) -> bool:
        if not self.email or not self.api_key:
            logger.info("Kalshi: no credentials -- public read-only mode (OK for paper)")
            return False
        data = self._http_post("/log-in", {"email": self.email, "password": self.api_key})
        if data and "token" in data:
            self.token = data["token"]
            self.token_expiry = time.time() + 1700
            logger.info("Kalshi: authenticated")
            return True
        return False

    def ensure_auth(self):
        if time.time() > self.token_expiry:
            self.login()

    def get_markets(self, status="open", limit=100, cursor=None) -> list:
        path = f"/markets?status={status}&limit={limit}"
        if cursor:
            path += f"&cursor={cursor}"
        data = self._http_get(path)
        if data is None:
            return []
        if "markets" in data:
            return data["markets"] or []
        logger.warning(f"Kalshi /markets response missing 'markets' key. Top-level keys: {list(data.keys())[:10]}")
        return []

    def get_market(self, market_id: str) -> Optional[dict]:
        data = self._http_get(f"/markets/{market_id}")
        if data and "market" in data:
            return data["market"]
        return None

    def get_orderbook(self, market_id: str) -> Optional[dict]:
        return self._http_get(f"/markets/{market_id}/orderbook")

    def get_market_price(self, market_id: str) -> Optional[float]:
        m = self.get_market(market_id)
        if not m:
            return None
        yb = (m.get("yes_bid") or 0) / 100.0
        ya = (m.get("yes_ask") or 0) / 100.0
        if yb > 0 and ya > 0:
            return (yb + ya) / 2
        lp = (m.get("last_price") or 0) / 100.0
        return lp if lp > 0 else None

    # -- Trading (paper-mode guards) --
    def place_order(self, market_id: str, side: str, price_cents: int, count: int) -> Optional[dict]:
        logger.warning(f"place_order called for {market_id} -- paper-mode build ignores live orders")
        return None

    def cancel_order(self, order_id: str) -> bool:
        return False

    def get_positions(self) -> list:
        return []

    def get_balance(self) -> Optional[float]:
        return None

    # -- Market scanning --
    def scan_markets_with_prices(self, limit=50) -> list:
        """
        Fetch markets with price data from the public /markets endpoint.
        Does NOT require auth or per-market orderbook round-trips.
        """
        markets = self.get_markets(limit=limit)
        if not markets:
            logger.warning("Kalshi /markets returned empty -- check network / API reachability")
            return []

        logger.info(f"Kalshi: /markets returned {len(markets)} raw markets")

        # One-shot diagnostic log of first market shape
        if not self._logged_sample and markets:
            sample = markets[0]
            logger.info(
                "Kalshi sample: ticker=%s yes_bid=%s yes_ask=%s last_price=%s volume=%s status=%s",
                sample.get("ticker"),
                sample.get("yes_bid"),
                sample.get("yes_ask"),
                sample.get("last_price"),
                sample.get("volume"),
                sample.get("status"),
            )
            self._logged_sample = True

        enriched = []
        dropped_no_price = 0
        for m in markets:
            try:
                market_id = m.get("ticker", "")
                if not market_id:
                    continue

                last_price = (m.get("last_price") or 0) / 100.0
                yes_ask = (m.get("yes_ask") or 0) / 100.0
                yes_bid = (m.get("yes_bid") or 0) / 100.0
                no_ask = (m.get("no_ask") or 0) / 100.0
                no_bid = (m.get("no_bid") or 0) / 100.0

                # Fallback chain: mid-bid-ask -> last_price -> ask -> bid -> 0.5 (default)
                if yes_bid > 0 and yes_ask > 0:
                    yes_price = (yes_bid + yes_ask) / 2
                elif last_price > 0:
                    yes_price = last_price
                elif yes_ask > 0:
                    yes_price = yes_ask
                elif yes_bid > 0:
                    yes_price = yes_bid
                elif no_bid > 0 or no_ask > 0:
                    # Derive from no-side
                    no_mid = (no_bid + no_ask) / 2 if (no_bid > 0 and no_ask > 0) else (no_ask or no_bid)
                    yes_price = max(0.01, 1.0 - no_mid)
                else:
                    dropped_no_price += 1
                    continue

                if no_bid > 0 and no_ask > 0:
                    no_price = (no_bid + no_ask) / 2
                else:
                    no_price = max(0.01, min(0.99, 1.0 - yes_price))

                enriched.append({
                    "platform": "kalshi",
                    "question": m.get("title", "") or m.get("subtitle", "") or market_id,
                    "market_id": market_id,
                    "event_ticker": m.get("event_ticker", ""),
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "volume": m.get("volume", 0) or 0,
                    "volume_24h": m.get("volume_24h", 0) or 0,
                    "open_interest": m.get("open_interest", 0) or 0,
                    "end_date": m.get("close_time", ""),
                    "raw": m,
                })
            except Exception as e:
                logger.debug(f"Skipping Kalshi market {m.get('ticker','?')}: {e}")
                continue

        logger.info(
            f"Kalshi: enriched {len(enriched)}/{len(markets)} markets "
            f"(dropped {dropped_no_price} for no-price, {len(markets)-len(enriched)-dropped_no_price} other)"
        )
        return enriched

    def is_connected(self) -> bool:
        data = self._http_get("/exchange/status")
        return data is not None
