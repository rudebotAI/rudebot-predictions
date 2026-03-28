"""
Kalshi API Connector -- v2
Uses Kalshi REST API with batch-friendly scanning.
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
    """Unified interface to Kalshi's API."""

    def __init__(self, config: dict):
        self.email = config.get("email", "")
        self.api_key = config.get("api_key", "")
        self.token = ""
        self.token_expiry = 0
        self._last_request = 0
        self._api_reachable = None  # None=untested, True/False=cached

    def _throttle(self):
        elapsed = time.time() - self._last_request
        if elapsed < 0.35:  # ~170 req/min (within Kalshi limits)
            time.sleep(0.35 - elapsed)
        self._last_request = time.time()

    def _get_headers(self) -> dict:
        """Auth headers for Kalshi API."""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _http_get(self, path: str, timeout: int = 3) -> Optional[dict]:
        if self._api_reachable is False:
            return None
        self._throttle()
        url = f"{KALSHI_API}{path}"
        try:
            req = urllib.request.Request(url, headers=self._get_headers())
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self._api_reachable = True
                return json.loads(resp.read().decode())
        except Exception as e:
            if self._api_reachable is None:
                self._api_reachable = False
                logger.warning(f"Kalshi API unreachable -- skipping all calls: {e}")
            else:
                logger.debug(f"Kalshi GET failed: {path} -- {e}")
            return None

    def _http_post(self, path: str, data: dict, timeout: int = 3) -> Optional[dict]:
        if self._api_reachable is False:
            return None
        self._throttle()
        url = f"{KALSHI_API}{path}"
        body = json.dumps(data).encode()
        try:
            req = urllib.request.Request(url, data=body, headers=self._get_headers(), method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self._api_reachable = True
                return json.loads(resp.read().decode())
        except Exception as e:
            if self._api_reachable is None:
                self._api_reachable = False
                logger.warning(f"Kalshi API unreachable -- skipping all calls: {e}")
            else:
                logger.debug(f"Kalshi POST failed: {path} -- {e}")
            return None

    # ── Auth ──

    def login(self) -> bool:
        """Authenticate and get bearer token."""
        if not self.email or not self.api_key:
            logger.warning("No Kalshi credentials -- read-only mode (public markets only)")
            return False
        try:
            data = self._http_post("/log-in", {"email": self.email, "password": self.api_key})
            if data and "token" in data:
                self.token = data["token"]
                self.token_expiry = time.time() + 1700
                logger.info("Kalshi authenticated")
                return True
            logger.error(f"Kalshi login failed: {data}")
            return False
        except Exception as e:
            logger.error(f"Kalshi login error: {e}")
            return False

    def ensure_auth(self):
        if time.time() > self.token_expiry:
            self.login()

    # ── Market Data ──

    def get_markets(self, status="open", limit=100) -> list:
        data = self._http_get(f"/markets?status={status}&limit={limit}")
        if data and "markets" in data:
            return data["markets"]
        return []

    def get_market(self, market_id: str) -> Optional[dict]:
        data = self._http_get(f"/markets/{market_id}")
        if data and "market" in data:
            return data["market"]
        return None

    def get_orderbook(self, market_id: str) -> Optional[dict]:
        return self._http_get(f"/markets/{market_id}/orderbook")

    def get_market_price(self, market_id: str) -> Optional[float]:
        book = self.get_orderbook(market_id)
        if not book:
            return None
        try:
            yes_bids = book.get("yes", [])
            if yes_bids:
                return max(b[0] for b in yes_bids) / 100
        except Exception:
            pass
        return None

    # ── Trading ──

    def place_order(self, market_id: str, side: str, price_cents: int, count: int) -> Optional[dict]:
        self.ensure_auth()
        if not self.token:
            logger.error("Cannot place order -- not authenticated")
            return None
        data = {
            "action": "buy" if side.lower() in ("buy", "yes") else "sell",
            "type": "limit",
            "side": "yes" if side.lower() in ("buy", "yes") else "no",
            "count": count,
            "yes_price": price_cents,
        }
        return self._http_post(f"/markets/{market_id}/orders", data)

    def cancel_order(self, order_id: str) -> bool:
        self.ensure_auth()
        try:
            self._http_post(f"/orders/{order_id}/cancel", {})
            return True
        except Exception as e:
            logger.error(f"Cancel failed: {e}")
            return False

    def get_positions(self) -> list:
        self.ensure_auth()
        data = self._http_get("/portfolio/positions")
        if data and "market_positions" in data:
            return data["market_positions"]
        return []

    def get_balance(self) -> Optional[float]:
        self.ensure_auth()
        data = self._http_get("/portfolio/balance")
        if data and "balance" in data:
            return data["balance"] / 100
        return None

    # ── Utility ──

    def scan_markets_with_prices(self, limit=50) -> list:
        """
        Fetch markets with price data. Uses last_price from the /markets
        endpoint to avoid N+1 orderbook calls. Only fetches orderbook
        for the top candidates (by volume) to get accurate spread data.
        """
        markets = self.get_markets(limit=limit)
        if not markets:
            logger.warning("Kalshi returned 0 markets")
            return []

        enriched = []
        for m in markets:
            try:
                market_id = m.get("ticker", "")
                last_price = (m.get("last_price", 0) or 0) / 100
                yes_ask = (m.get("yes_ask", 0) or 0) / 100
                yes_bid = (m.get("yes_bid", 0) or 0) / 100
                no_ask = (m.get("no_ask", 0) or 0) / 100
                no_bid = (m.get("no_bid", 0) or 0) / 100

                # Use bid/ask midpoint if available, else last_price
                if yes_bid > 0 and yes_ask > 0:
                    yes_price = (yes_bid + yes_ask) / 2
                elif last_price > 0:
                    yes_price = last_price
                else:
                    continue  # No price data at all

                if no_bid > 0 and no_ask > 0:
                    no_price = (no_bid + no_ask) / 2
                else:
                    no_price = 1 - yes_price if yes_price else None

                enriched.append({
                    "platform": "kalshi",
                    "question": m.get("title", ""),
                    "market_id": market_id,
                    "event_ticker": m.get("event_ticker", ""),
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "volume": m.get("volume", 0),
                    "volume_24h": m.get("volume_24h", 0),
                    "open_interest": m.get("open_interest", 0),
                    "end_date": m.get("close_time", ""),
                    "raw": m,
                })
            except Exception as e:
                logger.debug(f"Skipping Kalshi market: {e}")
                continue

        logger.info(f"Kalshi: enriched {len(enriched)} markets (no orderbook round-trips)")
        return enriched

    def is_connected(self) -> bool:
        data = self._http_get("/exchange/status")
        return data is not None
