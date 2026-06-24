"""
Polymarket reference connector -- READ-ONLY price feed.

Used ONLY as a cross-venue price reference for the scanner's divergence
signal. The bot does NOT trade on Polymarket; it never authenticates, never
sends an order here. Public Gamma API only. Paper-mode safe by construction.

Polymarket relaunched CFTC-regulated for US users (late 2025); this module
reads public market prices, which requires no account.
"""
import json
import time
import logging
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"

def _to_float(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default

class PolymarketReference:
    """Read-only public price reference. No auth, no trading."""

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.limit = int(cfg.get("reference_limit", 200))
        self._last = 0.0

    def _throttle(self):
        dt = time.time() - self._last
        if dt < 0.2:
            time.sleep(0.2 - dt)
        self._last = time.time()

    def _get(self, path: str, timeout: int = 10) -> Optional[object]:
        self._throttle()
        url = f"{GAMMA_API}{path}"
        try:
            req = urllib.request.Request(
                url, headers={"Accept": "application/json",
                              "User-Agent": "rudebot-predictions/5.0 (reference)"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            logger.debug(f"Polymarket GET {path} -> HTTP {e.code}: {e.reason}")
        except Exception as e:
            logger.debug(f"Polymarket GET {path} failed: {type(e).__name__}: {e}")
        return None

    def _parse_yes_price(self, m: dict) -> Optional[float]:
        """Yes-side probability from a Gamma market. Handles the common shapes:
        outcomePrices as JSON-string or list, paired with outcomes."""
        outcomes = m.get("outcomes")
        prices = m.get("outcomePrices")
        if isinstance(outcomes, str):
            try: outcomes = json.loads(outcomes)
            except Exception: outcomes = None
        if isinstance(prices, str):
            try: prices = json.loads(prices)
            except Exception: prices = None
        if isinstance(outcomes, list) and isinstance(prices, list) and len(outcomes) == len(prices):
            for o, p in zip(outcomes, prices):
                if str(o).strip().lower() in ("yes", "true"):
                    v = _to_float(p)
                    if v is not None:
                        return v
            # binary market without explicit Yes label: take first leg
            v = _to_float(prices[0]) if prices else None
            return v
        # single-number fallbacks
        for k in ("lastTradePrice", "bestBid", "price"):
            v = _to_float(m.get(k))
            if v is not None:
                return v
        return None

    def fetch_reference_markets(self) -> list:
        """Return [{question, yes_price, end_date, volume, liquidity}] for active,
        non-closed, binary Polymarket markets. Read-only."""
        if not self.enabled:
            return []
        out = []
        data = self._get(f"/markets?active=true&closed=false&limit={self.limit}")
        if not isinstance(data, list):
            # some deployments wrap as {"data":[...]}
            data = (data or {}).get("data", []) if isinstance(data, dict) else []
        for m in data or []:
            yp = self._parse_yes_price(m)
            if yp is None or not (0.0 < yp < 1.0):
                continue
            out.append({
                "platform": "polymarket",
                "question": m.get("question") or m.get("title") or "",
                "yes_price": round(yp, 4),
                "end_date": m.get("endDate") or m.get("end_date_iso") or "",
                "volume": _to_float(m.get("volume"), 0.0),
                "liquidity": _to_float(m.get("liquidity"), 0.0),
            })
        logger.info(f"Polymarket reference: fetched {len(out)} active binary markets")
        return out
