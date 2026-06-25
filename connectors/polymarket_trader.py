"""
Polymarket US (regulated) TRADING connector -- api.polymarket.us, Ed25519-signed.

SEPARATE from connectors/polymarket.py (which is the read-only price reference).
This module places REAL orders on the CFTC-regulated Polymarket US venue.

HARD-GATED OFF BY DEFAULT. It will refuse to place any order unless ALL of:
  - env POLYMARKET_LIVE=1
  - env POLYMARKET_API_KEY  (Ed25519 key id from the Polymarket US developer portal)
  - env POLYMARKET_PRIVATE_KEY (Ed25519 private key, base64 or PEM)
are present. Mirrors connectors/kalshi.py's interface so the executor/router
can treat both venues uniformly.

!! PHASE-0 VERIFY !!  The exact canonical signing string and order payload for
api.polymarket.us must be confirmed against the official Polymarket US SDK with
real credentials before this is enabled for live trading. Until verified, keep
POLYMARKET_LIVE unset so place_order() is a no-op that returns an error.
"""
import os
import json
import time
import base64
import logging
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger(__name__)

POLYMARKET_US_API = os.getenv("POLYMARKET_US_API", "https://api.polymarket.us")

def _to_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default

def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")

class PolymarketTrader:
    """Live trading client for Polymarket US. Refuses to trade unless explicitly
    armed via env. No crypto wallet -- regulated fiat venue, API-key auth."""

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        self.api_key = cfg.get("api_key") or os.getenv("POLYMARKET_API_KEY", "")
        self._private_key_raw = cfg.get("private_key") or os.getenv("POLYMARKET_PRIVATE_KEY", "")
        self.live_armed = _env_truthy("POLYMARKET_LIVE")
        self._signer = None
        self._last_request = 0.0
        if self.api_key and self._private_key_raw:
            try:
                self._signer = self._load_ed25519(self._private_key_raw)
                logger.info("Polymarket US: trading auth configured (Ed25519 key loaded)")
            except Exception as e:
                logger.error(
                    "Polymarket US: failed to load Ed25519 key -- trading disabled: "
                    f"{type(e).__name__}: {e}"
                )
                self._signer = None

    # -- auth --
    def _load_ed25519(self, raw):
        """Load an Ed25519 private key. Accepts PEM or bare base64 (32/64 bytes)."""
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        text = raw.decode() if isinstance(raw, bytes) else raw
        text = text.strip()
        if "BEGIN" in text:
            return load_pem_private_key(text.encode(), password=None)
        # bare base64 seed
        seed = base64.b64decode(text)
        if len(seed) > 32:
            seed = seed[:32]
        return Ed25519PrivateKey.from_private_bytes(seed)

    def has_trading_auth(self) -> bool:
        """True only when armed AND credentials are present/loaded.
        This is the master safety gate -- the executor checks it before ANY order."""
        return bool(self.live_armed and self.api_key and self._signer is not None)

    def _sign(self, timestamp_ms: str, method: str, path: str, body: str = "") -> str:
        # !! PHASE-0 VERIFY !! canonical message format per Polymarket US SDK.
        message = f"{timestamp_ms}{method}{path}{body}".encode()
        sig = self._signer.sign(message)
        return base64.b64encode(sig).decode()

    def _signed_headers(self, method: str, path: str, body: str = "") -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "rudebot-predictions/6.0",
            "PM-ACCESS-KEY": self.api_key,
            "PM-ACCESS-TIMESTAMP": ts,
            "PM-ACCESS-SIGNATURE": self._sign(ts, method, path, body),
        }

    def _throttle(self):
        dt = time.time() - self._last_request
        if dt < 0.15:
            time.sleep(0.15 - dt)
        self._last_request = time.time()

    def _signed_request(self, method: str, path: str, body: Optional[dict] = None,
                        timeout: int = 15) -> Optional[dict]:
        if not self.has_trading_auth():
            logger.error(f"Polymarket {method} {path}: not armed / no trading auth")
            return None
        self._throttle()
        data = json.dumps(body) if body is not None else ""
        url = f"{POLYMARKET_US_API}{path}"
        try:
            req = urllib.request.Request(
                url, data=data.encode() if data else None,
                headers=self._signed_headers(method, path, data), method=method,
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = ""
            try: detail = e.read().decode()[:300]
            except Exception: pass
            logger.warning(f"Polymarket {method} {path} -> HTTP {e.code}: {e.reason} {detail}")
        except Exception as e:
            logger.warning(f"Polymarket {method} {path} failed: {type(e).__name__}: {e}")
        return None

    # -- trading (all refuse unless armed) --
    def get_balance(self) -> Optional[float]:
        resp = self._signed_request("GET", "/v1/portfolio/balance")
        if not resp:
            return None
        return _to_float(resp.get("available", resp.get("balance")))

    def get_positions(self) -> list:
        resp = self._signed_request("GET", "/v1/portfolio/positions")
        return (resp or {}).get("positions", []) if isinstance(resp, dict) else []

    def place_order(self, market_id: str, outcome_id: str, side: str,
                    price: float, size_usd: float, order_type: str = "limit") -> Optional[dict]:
        """Place a REAL order on Polymarket US. Returns parsed response or None.
        Refuses unless has_trading_auth() (armed + creds)."""
        if not self.has_trading_auth():
            logger.error(f"place_order({market_id}) refused: Polymarket US not armed / no auth")
            return {"error": "polymarket not armed"}
        side = side.lower()
        if side not in ("buy", "sell"):
            return {"error": f"invalid side {side!r}"}
        if not (0 < price < 1) or size_usd <= 0:
            return {"error": f"invalid price/size (price={price}, size_usd={size_usd})"}
        body = {
            "market_id": market_id,
            "outcome_id": outcome_id,
            "side": side,
            "type": order_type,
            "price": round(price, 4),
            "size": round(size_usd, 2),
        }
        resp = self._signed_request("POST", "/v1/order", body)
        if resp is None:
            return None
        return resp.get("order", resp) if isinstance(resp, dict) else resp

    def cancel_order(self, order_id: str) -> bool:
        resp = self._signed_request("DELETE", f"/v1/order/{order_id}")
        return resp is not None

    # -- tradable market discovery (public, no auth) --
    def fetch_tradable_markets(self, limit: int = 100) -> list:
        """Public Gamma read -> tradable Polymarket markets tagged for the scanner.
        Returns dicts with market_id + outcome_id so a confirmed trade can be
        placed via place_order(). No auth needed (read-only); trading stays gated."""
        import urllib.request as _u
        out = []
        try:
            url = ("https://gamma-api.polymarket.com/markets"
                   f"?active=true&closed=false&limit={limit}")
            req = _u.Request(url, headers={"Accept": "application/json",
                                           "User-Agent": "rudebot-predictions/6.0"})
            with _u.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            logger.debug(f"Polymarket tradable fetch failed: {type(e).__name__}: {e}")
            return []
        if isinstance(data, dict):
            data = data.get("data", [])
        for m in data or []:
            outcomes = m.get("outcomes"); prices = m.get("outcomePrices")
            tokens = m.get("clobTokenIds")
            if isinstance(outcomes, str):
                try: outcomes = json.loads(outcomes)
                except Exception: outcomes = None
            if isinstance(prices, str):
                try: prices = json.loads(prices)
                except Exception: prices = None
            if isinstance(tokens, str):
                try: tokens = json.loads(tokens)
                except Exception: tokens = None
            if not (isinstance(outcomes, list) and isinstance(prices, list)):
                continue
            yes_i = next((i for i, o in enumerate(outcomes)
                          if str(o).strip().lower() in ("yes", "true")), 0)
            yp = _to_float(prices[yes_i]) if yes_i < len(prices) else 0.0
            if not (0.0 < yp < 1.0):
                continue
            outcome_id = ""
            if isinstance(tokens, list) and yes_i < len(tokens):
                outcome_id = str(tokens[yes_i])
            out.append({
                "platform": "polymarket",
                "question": m.get("question") or m.get("title") or "",
                "market_id": str(m.get("conditionId") or m.get("id") or ""),
                "outcome_id": outcome_id,
                "yes_price": round(yp, 4),
                "no_price": round(max(0.01, min(0.99, 1.0 - yp)), 4),
                "volume_24h": _to_float(m.get("volume24hr") or m.get("volume"), 0.0),
                "volume": _to_float(m.get("volume"), 0.0),
                "end_date": m.get("endDate") or "",
            })
        logger.info(f"Polymarket: {len(out)} tradable markets discovered")
        return out
