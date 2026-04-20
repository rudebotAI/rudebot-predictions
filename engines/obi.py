"""
Orderbook Imbalance Strategy (U7)

Computes the Order Book Imbalance (OBI) ratio at a 500ms refresh and
emits fade signals when the dominant side crosses a configurable
threshold. Self-contained — no external data required.

OBI = bid_volume / (bid_volume + ask_volume) at top N levels.
  * OBI >= threshold     → heavy bids → fade by selling (or shorting YES)
  * OBI <= 1 - threshold → heavy asks → fade by buying

Safety:
  * enabled: False by default.
  * Per-trade cap and depth guard apply.
  * Signal-only: routed through Telegram confirm before any order.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "threshold": 0.60,           # fire at 60% imbalance
    "levels": 3,                 # top 3 levels on each side
    "min_top_depth_usd": 250.0,  # both sides must have this much at the top
    "max_usd_per_trade": 5.0,
    "refresh_ms": 500,
}


class OBIStrategy:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = {**DEFAULTS, **(cfg.get("obi") or {})}

    def evaluate(self, book: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Evaluate a single orderbook snapshot and return a signal or None.

        Expected book shape:
            {
              "market_id": str,
              "bids": [[price, size], ...],   # descending price
              "asks": [[price, size], ...],   # ascending price
            }
        """
        if not self.cfg.get("enabled", False):
            return None

        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None

        n = self.cfg["levels"]
        bid_vol = sum(float(p) * float(s) for p, s in bids[:n])
        ask_vol = sum(float(p) * float(s) for p, s in asks[:n])

        if bid_vol < self.cfg["min_top_depth_usd"] or ask_vol < self.cfg["min_top_depth_usd"]:
            return None

        total = bid_vol + ask_vol
        if total <= 0:
            return None
        obi = bid_vol / total

        thr = self.cfg["threshold"]
        if obi >= thr:
            # Fade the heavy bid → SELL YES (or BUY NO). We model as SELL at the best bid.
            best_bid = float(bids[0][0])
            return self._build_signal(book, side="SELL", price=best_bid, obi=obi)
        if obi <= 1 - thr:
            best_ask = float(asks[0][0])
            return self._build_signal(book, side="BUY", price=best_ask, obi=obi)

        return None

    def _build_signal(self, book: Dict[str, Any], side: str, price: float, obi: float) -> Dict[str, Any]:
        return {
            "kind": "obi_fade",
            "source": "obi",
            "market_id": book.get("market_id"),
            "side": side,
            "price": price,
            "usd": self.cfg["max_usd_per_trade"],
            "obi": round(obi, 3),
            "note": "awaiting Telegram confirm",
        }
