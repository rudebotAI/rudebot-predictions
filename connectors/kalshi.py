"""
Kalshi API Connector -- v4 orderbook-enrichment
Fetches /markets then /markets/{ticker}/orderbook per market for prices.
Works without auth. Paper-mode safe.
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
    """Unified interface to Kalshi's public API (paper-mode safe, no auth needed)."""

    def __init__(self, config: dict):
        self.email = config.get("email", "")
        self.api_key = config.get("api_key", "")
        self.token = ""
        self.token_expiry = 0
        self._last_request = 0
        self._logged_sample = False

    def _throttle(self):
        elapsed = time.time() - self._last_request
        if elapsed < 0.12:
            time.sleep(0.12 - elapsed)
        self._last_request = time.time()

    def _get_headers(self) -> dict:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "rudebot-predictions/4.2 (paper-mode)",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _http_get(self, path: str, timeout: int = 10) -> Optional[dict]:
        self._throttle()
        url = f"{KALSHI_API}{path}"
        try:
            req = urllib.request.Request(url, headers=self._get_headers())
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode()
                return json.loads(body)
        except urllib.error.HTTPError as e:
            logger.debug(f"Kalshi GET {path} -> HTTP {e.code}: {e.reason}")
            return None
        except urllib.error.URLError as e:
            logger.debug(f"Kalshi GET {path} network error: {e.reason}")
            return None
        except Exception as e:
            logger.debug(f"Kalshi GET {path} failed: {type(e).__name__}: {e}")
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
        logger.info("Kalshi: login failed, continuing public read-only")
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
        ob = self.get_orderbook(market_id)
        if not ob:
            return None
        yb, ya = self._ob_best_yes(ob)
        if yb and ya:
            return (yb + ya) / 2
        return yb or ya or None

    @staticmethod
    def _ob_best_yes(ob_response: dict):
        """Return (best_yes_bid, best_yes_ask) as fractions in [0,1]."""
        ob = ob_response.get("orderbook") if isinstance(ob_response, dict) else None
        if not isinstance(ob, dict):
            return None, None
        yes_levels = ob.get("yes") or []
        no_levels = ob.get("no") or []
        best_yes_bid = None
        if yes_levels:
            try:
                best_yes_bid = max(lvl[0] for lvl in yes_levels if lvl and lvl[0] is not None) / 100.0
            except Exception:
                best_yes_bid = None
        best_yes_ask = None
        if no_levels:
            try:
                best_no_bid_cents = max(lvl[0] for lvl in no_levels if lvl and lvl[0] is not None)
                best_yes_ask = (100 - best_no_bid_cents) / 100.0
            except Exception:
                best_yes_ask = None
        return best_yes_bid, best_yes_ask

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
    def scan_markets_with_prices(self, limit=30) -> list:
        """
        Fetch markets then enrich with per-market orderbook prices.
        Works without auth. Costs limit+1 HTTP calls per scan.
        """
        markets = self.get_markets(limit=limit)
        if not markets:
            logger.warning("Kalshi /markets returned empty -- check network / API reachability")
            return []

        logger.info(f"Kalshi: /markets returned {len(markets)} raw markets; enriching via orderbook")

        if not self._logged_sample and markets:
            sample = markets[0]
            logger.info(
                "Kalshi sample: ticker=%s status=%s volume=%s",
                sample.get("ticker"), sample.get("status"), sample.get("volume"),
            )
            self._logged_sample = True

        enriched = []
        dropped_no_price = 0
        dropped_no_ob = 0
        for m in markets:
            try:
                market_id = m.get("ticker", "")
                if not market_id:
                    continue

                ob_resp = self.get_orderbook(market_id)
                if not ob_resp:
                    dropped_no_ob += 1
                    continue

                yes_bid, yes_ask = self._ob_best_yes(ob_resp)

                # Fallback to market-level price fields if orderbook is empty
                if yes_bid is None and yes_ask is None:
                    lp = (m.get("last_price") or 0) / 100.0
                    mb = (m.get("yes_bid") or 0) / 100.0
                    ma = (m.get("yes_ask") or 0) / 100.0
                    if mb > 0 and ma > 0:
                        yes_bid, yes_ask = mb, ma
                    elif lp > 0:
                        yes_bid = yes_ask = lp

                if yes_bid and yes_ask:
                    yes_price = (yes_bid + yes_ask) / 2
                elif yes_bid:
                    yes_price = yes_bid
                elif yes_ask:
                    yes_price = yes_ask
                else:
                    dropped_no_price += 1
                    continue

                no_price = max(0.01, min(0.99, 1.0 - yes_price))

                enriched.append({
                    "platform": "kalshi",
                    "question": m.get("title", "") or m.get("subtitle", "") or market_id,
                    "market_id": market_id,
                    "event_ticker": m.get("event_ticker", ""),
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "yes_bid": yes_bid or 0.0,
                    "yes_ask": yes_ask or 0.0,
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
            f"(dropped {dropped_no_price} no-price, {dropped_no_ob} no-orderbook)"
        )
        return enriched

    def is_connected(self) -> bool:
        data = self._http_get("/exchange/status")
        return data is not None
