"""
Kalshi API Connector -- v5 event-based scan with new API field names
Fetches /events then /markets?event_ticker=X per event.
Handles new Kalshi field names: yes_bid_dollars, yes_ask_dollars (string decimals),
volume_fp, volume_24h_fp, open_interest_fp, last_price_dollars.
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


def _to_float(x, default=0.0):
    if x is None:
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


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
        if elapsed < 0.1:
            time.sleep(0.1 - elapsed)
        self._last_request = time.time()

    def _get_headers(self) -> dict:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "rudebot-predictions/5.0 (paper-mode)",
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
                return json.loads(resp.read().decode())
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

    def get_events(self, status="open", limit=50, cursor=None) -> list:
        path = f"/events?status={status}&limit={limit}&with_nested_markets=false"
        if cursor:
            path += f"&cursor={cursor}"
        data = self._http_get(path)
        if data is None:
            return []
        return data.get("events", []) or []

    def get_markets(self, status="open", limit=100, cursor=None, event_ticker=None) -> list:
        path = f"/markets?status={status}&limit={limit}"
        if cursor:
            path += f"&cursor={cursor}"
        if event_ticker:
            path += f"&event_ticker={event_ticker}"
        data = self._http_get(path)
        if data is None:
            return []
        if "markets" in data:
            return data["markets"] or []
        logger.warning(f"Kalshi /markets response missing 'markets' key. Keys: {list(data.keys())[:10]}")
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
        yb = _to_float(m.get("yes_bid_dollars"))
        ya = _to_float(m.get("yes_ask_dollars"))
        if yb > 0 and ya > 0:
            return (yb + ya) / 2
        lp = _to_float(m.get("last_price_dollars"))
        return lp or yb or ya or None

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
        Event-based scan: fetch /events, then /markets?event_ticker for each.
        Kalshi's /markets without event filter is dominated by zero-volume parlay markets,
        so event-scoped queries are what actually return tradable markets.
        Parses new Kalshi field names (yes_bid_dollars, volume_24h_fp, etc.).
        """
        events = self.get_events(limit=50)
        if not events:
            logger.warning("Kalshi /events returned empty -- check network / API reachability")
            return []

        logger.info(f"Kalshi: /events returned {len(events)} events; scanning per-event markets")

        enriched = []
        events_with_markets = 0
        markets_seen = 0
        markets_dropped_no_price = 0
        markets_dropped_no_volume = 0

        for ev in events:
            if len(enriched) >= limit:
                break
            event_ticker = ev.get("event_ticker", "")
            if not event_ticker:
                continue
            ms = self.get_markets(event_ticker=event_ticker, limit=50)
            if not ms:
                continue
            events_with_markets += 1
            for m in ms:
                if len(enriched) >= limit:
                    break
                markets_seen += 1
                try:
                    market_id = m.get("ticker", "")
                    if not market_id:
                        continue

                    yb = _to_float(m.get("yes_bid_dollars"))
                    ya = _to_float(m.get("yes_ask_dollars"))
                    lp = _to_float(m.get("last_price_dollars"))
                    vol_24h = _to_float(m.get("volume_24h_fp"))
                    vol_total = _to_float(m.get("volume_fp"))
                    oi = _to_float(m.get("open_interest_fp"))
                    liq = _to_float(m.get("liquidity_dollars"))

                    # Skip markets with no liquidity/activity -- can't realistically trade them
                    if vol_total <= 0 and oi <= 0:
                        markets_dropped_no_volume += 1
                        continue

                    if yb > 0 and ya > 0:
                        yes_price = (yb + ya) / 2
                    elif yb > 0:
                        yes_price = yb
                    elif ya > 0:
                        yes_price = ya
                    elif lp > 0:
                        yes_price = lp
                    else:
                        markets_dropped_no_price += 1
                        continue

                    no_price = max(0.01, min(0.99, 1.0 - yes_price))

                    if not self._logged_sample:
                        logger.info(
                            "Kalshi sample: ticker=%s yb=%s ya=%s lp=%s vol24=%s",
                            market_id, yb, ya, lp, vol_24h,
                        )
                        self._logged_sample = True

                    enriched.append({
                        "platform": "kalshi",
                        "question": m.get("title", "") or m.get("subtitle", "") or market_id,
                        "market_id": market_id,
                        "event_ticker": event_ticker,
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "yes_bid": yb,
                        "yes_ask": ya,
                        "volume": vol_total,
                        "volume_24h": vol_24h,
                        "open_interest": oi,
                        "liquidity": liq,
                        "end_date": m.get("close_time", ""),
                        "raw": m,
                    })
                except Exception as e:
                    logger.debug(f"Skipping Kalshi market {m.get('ticker','?')}: {e}")
                    continue

        logger.info(
            f"Kalshi: enriched {len(enriched)} markets from {events_with_markets}/{len(events)} events "
            f"(saw {markets_seen}, dropped {markets_dropped_no_volume} zero-volume, "
            f"{markets_dropped_no_price} no-price)"
        )
        return enriched

    def is_connected(self) -> bool:
        data = self._http_get("/exchange/status")
        return data is not None
