"""
Kalshi API Connector -- v6 (Sept 2026 API surface)

What changed vs v5:
- Orders go through the V2 endpoint `POST /portfolio/events/orders`
  (legacy `/portfolio/orders` mutations were deprecated June 2026).
  V2 uses book-side vocabulary (`bid` == long YES, `ask` == long NO), a
  single yes-leg `price` in fixed-point dollars, and fixed-point contract
  counts. `time_in_force` and `self_trade_prevention_type` are required.
- Prices are handled as dollars end-to-end (no more integer cents). Every
  order price is snapped to the market's `price_ranges` grid, which is the
  documented source of truth for valid ticks (sub-penny structures exist).
- Fill verification: V2 create returns `fill_count` / `remaining_count` /
  `average_fill_price`; `get_order()` exposes status (resting / canceled /
  executed) for anything that rested.
- Legacy email+password `/log-in` flow removed (dead since RSA keys).
- 429 handling: exponential backoff on the token-bucket limiter.
- Balance prefers `balance_dollars` (centi-cent precision), positions parse
  `position_fp` / `market_exposure_dollars`.

Public market data still needs no auth, so paper mode is unaffected.
"""
import json
import time
import logging
import urllib.request
import urllib.error
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN, ROUND_UP
from typing import Optional

logger = logging.getLogger(__name__)

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
API_PREFIX = "/trade-api/v2"
USER_AGENT = "rudebot-predictions/6.0"

# Default price grid when a market payload has no price_ranges (older
# markets are whole-cent). Whole-cent prices are valid in every structure.
DEFAULT_PRICE_RANGES = [{"start": "0.00", "end": "1.00", "step": "0.01"}]

# 429 backoff: Kalshi's bucket refills continuously and sends no Retry-After.
_BACKOFF_BASE = 0.25
_BACKOFF_MAX = 4.0
_MAX_RETRIES = 4


def _to_float(x, default=0.0):
    if x is None:
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _fp(x, places: int = 2) -> str:
    """Fixed-point string with `places` decimals (Kalshi *_fp / *_dollars)."""
    q = Decimal(1).scaleb(-places)
    return str(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP))


def snap_price(price: float, price_ranges: Optional[list], side: str = "buy") -> float:
    """Snap a yes-leg dollar price onto the market's valid tick grid.

    `price_ranges` is the market's `[{start, end, step}]` array. For a buy we
    round DOWN to the grid (never pay more than intended); for a sell we round
    UP (never receive less). Falls back to whole cents when no grid is given.
    """
    ranges = price_ranges or DEFAULT_PRICE_RANGES
    p = Decimal(str(price))
    p = max(Decimal("0"), min(Decimal("1"), p))
    band = None
    for r in ranges:
        start, end = Decimal(str(r.get("start", "0"))), Decimal(str(r.get("end", "1")))
        if start <= p <= end:
            band = r
            break
    if band is None:
        band = ranges[-1] if p > Decimal("0.5") else ranges[0]
    step = Decimal(str(band.get("step", "0.01")))
    start = Decimal(str(band.get("start", "0")))
    end = Decimal(str(band.get("end", "1")))
    rounding = ROUND_DOWN if side == "buy" else ROUND_UP
    n = ((p - start) / step).quantize(Decimal(1), rounding=rounding)
    snapped = start + n * step
    snapped = max(start, min(end, snapped))
    return float(snapped)


def _load_private_key_tolerant(pem):
    """Load an RSA private key from PEM, tolerating common env-var paste damage.

    Accepts: a full PEM (with armor), OR a bare base64 key body that lost its
    -----BEGIN/END----- lines during copy-paste. For a bare body we try the
    standard armors (PKCS#1 RSA, then PKCS#8, then EC) and use whichever
    deserializes. Never weakens auth -- it only restores framing the key
    already had.
    """
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    text = (pem.decode() if isinstance(pem, bytes) else pem).strip()
    candidates = []
    if "BEGIN" in text:
        candidates.append(text)
    else:
        for hdr in ("RSA PRIVATE KEY", "PRIVATE KEY", "EC PRIVATE KEY"):
            candidates.append(f"-----BEGIN {hdr}-----\n{text}\n-----END {hdr}-----\n")

    last_err = None
    for cand in candidates:
        try:
            return load_pem_private_key(cand.encode(), password=None)
        except Exception as e:  # try the next framing
            last_err = e
    raise last_err if last_err else ValueError("empty private key")


class KalshiConnector:
    """Unified interface to Kalshi's trade-api/v2 (paper-mode safe, no auth needed)."""

    def __init__(self, config: dict):
        self._last_request = 0
        self._logged_sample = False

        if config.get("email") or config.get("api_key"):
            logger.warning(
                "Kalshi: KALSHI_EMAIL/KALSHI_API_KEY are ignored -- the legacy "
                "/log-in flow is gone. Use KALSHI_ACCESS_KEY + KALSHI_PRIVATE_KEY."
            )

        # --- Trading auth (RSA-PSS request signing) ---
        # Requests are signed with RSA-PSS(SHA256) over
        # "<timestamp_ms><METHOD><path-without-query>".
        self.access_key = config.get("access_key", "")
        self._private_key = None
        pem = config.get("private_key", "") or ""
        if self.access_key and pem:
            try:
                self._private_key = _load_private_key_tolerant(pem)
                logger.info("Kalshi: trading auth configured (RSA key loaded)")
            except Exception as e:
                logger.error(
                    "Kalshi: failed to load private key -- live trading disabled: "
                    f"{type(e).__name__}: {e}"
                )
                self._private_key = None

    # ------------------------------------------------------------------
    # Auth / signing
    # ------------------------------------------------------------------
    def has_trading_auth(self) -> bool:
        """True only when an API key ID and a usable RSA private key are present."""
        return bool(self.access_key and self._private_key is not None)

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """RSA-PSS(SHA256) signature of '<ts><METHOD><path>', base64-encoded.

        `path` must be the full request path including the /trade-api/v2 prefix.
        Any query string is stripped here, per Kalshi's spec.
        """
        import base64
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        path = path.split("?", 1)[0]
        message = f"{timestamp_ms}{method}{path}".encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()

    def _signed_headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        sign_path = f"{API_PREFIX}{path}"
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "KALSHI-ACCESS-KEY": self.access_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, sign_path),
        }

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _throttle(self):
        elapsed = time.time() - self._last_request
        if elapsed < 0.1:
            time.sleep(0.1 - elapsed)
        self._last_request = time.time()

    def _public_headers(self) -> dict:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"{USER_AGENT} (public)",
        }

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 signed: bool = False, timeout: int = 15,
                 retry_on_429: bool = True) -> Optional[dict]:
        """Single HTTP call with 429 backoff. Returns parsed JSON, {} for an
        empty body, or None on failure. Failures are logged at WARNING for
        signed (trading) calls and DEBUG for public reads."""
        url = f"{KALSHI_API}{path}"
        data = json.dumps(body).encode() if body is not None else None
        level = logging.WARNING if signed else logging.DEBUG

        for attempt in range(_MAX_RETRIES + 1):
            self._throttle()
            headers = self._signed_headers(method, path) if signed else self._public_headers()
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode()[:300]
                except Exception:
                    pass
                if e.code == 429 and retry_on_429 and attempt < _MAX_RETRIES:
                    delay = min(_BACKOFF_MAX, _BACKOFF_BASE * (2 ** attempt))
                    logger.log(level, f"Kalshi {method} {path} -> 429, backing off {delay:.2f}s")
                    time.sleep(delay)
                    continue
                logger.log(level, f"Kalshi {method} {path} -> HTTP {e.code}: {e.reason} {detail}")
                return None
            except urllib.error.URLError as e:
                logger.log(level, f"Kalshi {method} {path} network error: {e.reason}")
                return None
            except Exception as e:
                logger.log(level, f"Kalshi {method} {path} failed: {type(e).__name__}: {e}")
                return None
        return None

    def _http_get(self, path: str, timeout: int = 10) -> Optional[dict]:
        return self._request("GET", path, signed=False, timeout=timeout)

    def _signed_request(self, method: str, path: str, body: Optional[dict] = None,
                        timeout: int = 15, retry_on_429: bool = True) -> Optional[dict]:
        """Make an RSA-signed authenticated request. Returns parsed JSON or None."""
        if not self.has_trading_auth():
            logger.error(f"Kalshi {method} {path}: no trading auth configured")
            return None
        return self._request(method, path, body=body, signed=True, timeout=timeout,
                             retry_on_429=retry_on_429)

    # ------------------------------------------------------------------
    # Public market data
    # ------------------------------------------------------------------
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

    @staticmethod
    def quote(market: dict) -> dict:
        """Normalize a raw market payload into yes-leg dollar quotes.

        Returns {yes_bid, yes_ask, mid, spread, ask_size, bid_size,
                 price_ranges}. Missing sides come back as 0.0.
        """
        yb = _to_float(market.get("yes_bid_dollars"))
        ya = _to_float(market.get("yes_ask_dollars"))
        lp = _to_float(market.get("last_price_dollars"))
        if yb > 0 and ya > 0:
            mid = (yb + ya) / 2
        else:
            mid = lp or yb or ya or 0.0
        return {
            "yes_bid": yb,
            "yes_ask": ya,
            "mid": mid,
            "spread": (ya - yb) if (yb > 0 and ya > 0) else None,
            "ask_size": _to_float(market.get("yes_ask_size_fp")),
            "bid_size": _to_float(market.get("yes_bid_size_fp")),
            "price_ranges": market.get("price_ranges") or DEFAULT_PRICE_RANGES,
        }

    def get_market_price(self, market_id: str) -> Optional[float]:
        m = self.get_market(market_id)
        if not m:
            return None
        return self.quote(m)["mid"] or None

    # ------------------------------------------------------------------
    # Trading (live, RSA-signed) -- V2 order surface
    # ------------------------------------------------------------------
    @staticmethod
    def to_yes_leg(side: str, price: float) -> float:
        """Convert a side-native price to the yes-leg price V2 expects.

        A NO contract bought at 0.30 is the same trade as a YES sold at 0.70.
        """
        return price if side == "yes" else 1.0 - price

    def build_order_body(self, market_id: str, side: str, price: float, count: float,
                         action: str = "buy", time_in_force: str = "immediate_or_cancel",
                         post_only: bool = False, client_order_id: Optional[str] = None,
                         expiration_time: Optional[int] = None, reduce_only: bool = False,
                         price_ranges: Optional[list] = None) -> dict:
        """Build (and validate) a V2 CreateOrder body. Raises ValueError.

        `side` is the outcome ("yes"/"no") and `price` is that side's own
        price in dollars; both are translated to V2's yes-leg book side.
        `action` "buy" opens exposure on `side`; "sell" closes it.
        """
        side = side.lower()
        action = action.lower()
        if side not in ("yes", "no"):
            raise ValueError(f"invalid side {side!r}")
        if action not in ("buy", "sell"):
            raise ValueError(f"invalid action {action!r}")
        if time_in_force not in ("fill_or_kill", "good_till_canceled", "immediate_or_cancel"):
            raise ValueError(f"invalid time_in_force {time_in_force!r}")
        if not (0 < price < 1):
            raise ValueError(f"invalid price {price} (must be strictly between 0 and 1)")
        if count is None or float(count) < 0.01:
            raise ValueError(f"invalid count {count} (min 0.01 contracts)")

        # Direction: buy-yes / sell-no -> long yes -> bid; buy-no / sell-yes -> ask.
        long_yes = (side == "yes") == (action == "buy")
        book_side = "bid" if long_yes else "ask"

        yes_leg = self.to_yes_leg(side, price)
        # A bid should never round up (overpay); an ask should never round down.
        snapped = snap_price(yes_leg, price_ranges, side="buy" if book_side == "bid" else "sell")
        if not (0 < snapped < 1):
            raise ValueError(f"price {price} snapped off the tradable grid ({snapped})")

        body = {
            "ticker": market_id,
            "side": book_side,
            "count": _fp(count, 2),
            "price": _fp(snapped, 4),
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": bool(post_only),
            "reduce_only": bool(reduce_only),
        }
        if client_order_id:
            body["client_order_id"] = str(client_order_id)
        if expiration_time and time_in_force == "good_till_canceled":
            body["expiration_time"] = int(expiration_time)
        return body

    def place_order(self, market_id: str, side: str, price: float, count: float,
                    action: str = "buy", time_in_force: str = "immediate_or_cancel",
                    post_only: bool = False, client_order_id: Optional[str] = None,
                    expiration_time: Optional[int] = None, reduce_only: bool = False,
                    price_ranges: Optional[list] = None) -> Optional[dict]:
        """Place a real order via V2 `POST /portfolio/events/orders`.

        Returns a normalized dict on success:
            {order_id, client_order_id, fill_count, remaining_count,
             average_fill_price, price (yes-leg sent), book_side, ts_ms, raw}
        or {"error": ...} for validation problems, or None when the request
        failed / auth is absent. Callers must treat fill_count as the truth
        about what actually executed -- an IOC order can fill 0..count.
        """
        if not self.has_trading_auth():
            logger.error(f"place_order({market_id}) refused: no Kalshi trading auth configured")
            return None
        try:
            body = self.build_order_body(
                market_id, side, price, count, action=action, time_in_force=time_in_force,
                post_only=post_only, client_order_id=client_order_id,
                expiration_time=expiration_time, reduce_only=reduce_only,
                price_ranges=price_ranges,
            )
        except ValueError as e:
            return {"error": str(e)}

        # Never auto-retry an order create on 429: a timeout-then-success would
        # double-fill. The caller can re-check by client_order_id instead.
        resp = self._signed_request("POST", "/portfolio/events/orders", body, retry_on_429=False)
        if resp is None:
            return None
        if not isinstance(resp, dict) or "order_id" not in resp:
            return {"error": f"unexpected V2 create response: {str(resp)[:200]}"}
        return {
            "order_id": resp.get("order_id"),
            "client_order_id": resp.get("client_order_id", body.get("client_order_id")),
            "fill_count": _to_float(resp.get("fill_count")),
            "remaining_count": _to_float(resp.get("remaining_count")),
            "average_fill_price": _to_float(resp.get("average_fill_price")) or None,
            "average_fee_paid": _to_float(resp.get("average_fee_paid")),
            "price": float(body["price"]),
            "book_side": body["side"],
            "ts_ms": resp.get("ts_ms"),
            "raw": resp,
        }

    def get_order(self, order_id: str) -> Optional[dict]:
        """GET /portfolio/orders/{id}. Status is resting | canceled | executed."""
        resp = self._signed_request("GET", f"/portfolio/orders/{order_id}")
        if not resp:
            return None
        o = resp.get("order", resp)
        return {
            "order_id": o.get("order_id"),
            "client_order_id": o.get("client_order_id"),
            "status": o.get("status"),
            "outcome_side": o.get("outcome_side"),
            "book_side": o.get("book_side"),
            "yes_price": _to_float(o.get("yes_price_dollars")),
            "fill_count": _to_float(o.get("fill_count_fp")),
            "remaining_count": _to_float(o.get("remaining_count_fp")),
            "initial_count": _to_float(o.get("initial_count_fp")),
            "raw": o,
        }

    def cancel_order(self, order_id: str, market_ticker: Optional[str] = None) -> bool:
        """V2 `DELETE /portfolio/events/orders/{id}` (auto-routed by market_ticker)."""
        path = f"/portfolio/events/orders/{order_id}"
        if market_ticker:
            path += f"?market_ticker={market_ticker}"
        resp = self._signed_request("DELETE", path)
        return resp is not None

    def get_positions(self) -> list:
        """Open market positions, normalized to dollars/contracts."""
        resp = self._signed_request("GET", "/portfolio/positions?count_filter=position&limit=200")
        if not resp:
            return []
        out = []
        for p in resp.get("market_positions", []) or []:
            out.append({
                "ticker": p.get("ticker"),
                "position": _to_float(p.get("position_fp", p.get("position"))),
                "exposure_usd": _to_float(p.get("market_exposure_dollars")),
                "realized_pnl_usd": _to_float(p.get("realized_pnl_dollars")),
                "fees_paid_usd": _to_float(p.get("fees_paid_dollars")),
                "raw": p,
            })
        return out

    def get_balance(self) -> Optional[float]:
        """Available balance in dollars. Prefers `balance_dollars` (4dp)."""
        resp = self._signed_request("GET", "/portfolio/balance")
        if not resp:
            return None
        if resp.get("balance_dollars") is not None:
            return _to_float(resp.get("balance_dollars"))
        if "balance" in resp:
            return _to_float(resp.get("balance")) / 100.0
        return None

    # ------------------------------------------------------------------
    # Market scanning
    # ------------------------------------------------------------------
    def scan_markets_with_prices(self, limit=30, per_event_cap=6, event_limit=100) -> list:
        """
        Event-based scan: fetch /events, then /markets?event_ticker for each.
        Kalshi's /markets without event filter is dominated by zero-volume parlay markets,
        so event-scoped queries are what actually return tradable markets.

        `per_event_cap` keeps one multi-strike event (e.g. 40 price ladders)
        from consuming the whole scan budget; markets within an event are
        taken in descending 24h-volume order.
        """
        events = self.get_events(limit=event_limit)
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
            ms.sort(key=lambda x: _to_float(x.get("volume_24h_fp")), reverse=True)
            taken_here = 0
            for m in ms:
                if len(enriched) >= limit or taken_here >= per_event_cap:
                    break
                markets_seen += 1
                try:
                    market_id = m.get("ticker", "")
                    if not market_id:
                        continue

                    q = self.quote(m)
                    vol_24h = _to_float(m.get("volume_24h_fp"))
                    vol_total = _to_float(m.get("volume_fp"))
                    oi = _to_float(m.get("open_interest_fp"))
                    liq = _to_float(m.get("liquidity_dollars"))

                    # Skip markets with no liquidity/activity -- can't realistically trade them
                    if vol_total <= 0 and oi <= 0:
                        markets_dropped_no_volume += 1
                        continue

                    yes_price = q["mid"]
                    if yes_price <= 0:
                        markets_dropped_no_price += 1
                        continue

                    # Use the real NO quote when present instead of assuming 1 - yes.
                    no_bid = _to_float(m.get("no_bid_dollars"))
                    no_ask = _to_float(m.get("no_ask_dollars"))
                    if no_bid > 0 and no_ask > 0:
                        no_price = (no_bid + no_ask) / 2
                    else:
                        no_price = 1.0 - yes_price
                    no_price = max(0.001, min(0.999, no_price))

                    if not self._logged_sample:
                        logger.info(
                            "Kalshi sample: ticker=%s yb=%s ya=%s spread=%s vol24=%s grid=%s",
                            market_id, q["yes_bid"], q["yes_ask"], q["spread"], vol_24h,
                            m.get("price_level_structure"),
                        )
                        self._logged_sample = True

                    taken_here += 1
                    enriched.append({
                        "platform": "kalshi",
                        "question": m.get("title", "") or m.get("subtitle", "") or market_id,
                        "market_id": market_id,
                        "event_ticker": event_ticker,
                        "exchange_index": m.get("exchange_index", 0),
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "yes_bid": q["yes_bid"],
                        "yes_ask": q["yes_ask"],
                        "spread": q["spread"],
                        "ask_size": q["ask_size"],
                        "bid_size": q["bid_size"],
                        "price_ranges": q["price_ranges"],
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
        return bool(data) and bool(data.get("exchange_active", True)) and bool(data.get("trading_active", True))
