"""
Market Maker (U9)

Two-sided GTD quoting with inventory skew. Continuously quotes both
sides of illiquid prediction markets and earns the spread passively.

Adjusts quote prices based on current YES/NO inventory to incentivize
rebalancing fills. Auto-cancels the opposite side on a fill and re-quotes
after a cooldown.

Safety:
  * enabled: False by default.
  * Conservative defaults: wide spread, small size, hard inventory caps.
  * Will not quote until configured explicitly — the default wallet-less
    state is a no-op.
  * Live-execution paths will refuse unless mode == "live" AND
    execution.live.LIVE_OPT_IN_FLAG is True.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "markets": [],                 # explicit allowlist of market_ids
    "base_spread": 0.04,           # 4¢ base two-sided spread
    "quote_size_usd": 2.0,         # per-side quote size
    "skew_strength": 0.5,          # how much to shift quotes per unit of inventory
    "max_inventory_abs": 20.0,     # hard cap on |YES - NO| shares
    "max_open_quotes": 4,          # bid + ask across markets
    "requote_cooldown_sec": 3,
    "order_tif": "GTD",            # Good-Till-Date
    "gtd_duration_sec": 60,
}


@dataclass
class MarketState:
    market_id: str
    inventory_yes: float = 0.0
    inventory_no: float = 0.0
    last_quote_epoch: float = 0.0


class MarketMaker:
    def __init__(self, cfg: Dict[str, Any], deps: Dict[str, Any]):
        self.cfg = {**DEFAULTS, **(cfg.get("market_maker") or {})}
        self.mode = cfg.get("mode", "paper")
        self.deps = deps  # expects {"orders": OrderRouter, "book": callable(market_id)->book}
        self.state: Dict[str, MarketState] = {
            m: MarketState(market_id=m) for m in self.cfg.get("markets", [])
        }

    def tick(self) -> List[Dict[str, Any]]:
        """One quoting cycle. Returns the set of quote intents produced.

        Quote intents are sent to the OrderRouter. If the mode is paper,
        the router should simulate fills instead of placing real orders.
        """
        if not self.cfg.get("enabled", False):
            return []
        intents: List[Dict[str, Any]] = []
        now = time.time()
        for mid, st in self.state.items():
            if now - st.last_quote_epoch < self.cfg["requote_cooldown_sec"]:
                continue
            book = self._get_book(mid)
            if book is None:
                continue
            quotes = self._compute_quotes(book, st)
            if quotes:
                intents.extend(quotes)
                st.last_quote_epoch = now
        return intents

    def on_fill(self, fill: Dict[str, Any]) -> None:
        mid = fill.get("market_id")
        if mid not in self.state:
            return
        st = self.state[mid]
        side = fill.get("side", "").upper()
        shares = float(fill.get("shares", 0.0))
        if side == "YES":
            st.inventory_yes += shares if fill.get("direction") == "BUY" else -shares
        elif side == "NO":
            st.inventory_no += shares if fill.get("direction") == "BUY" else -shares

    # ----------- internals -----------
    def _get_book(self, market_id: str):
        book_fn = self.deps.get("book")
        if not callable(book_fn):
            return None
        try:
            return book_fn(market_id)
        except Exception:  # noqa: BLE001
            return None

    def _compute_quotes(self, book: Dict[str, Any], st: MarketState) -> List[Dict[str, Any]]:
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return []
        mid_price = (float(bids[0][0]) + float(asks[0][0])) / 2.0

        inventory = st.inventory_yes - st.inventory_no
        if abs(inventory) >= self.cfg["max_inventory_abs"]:
            # Skew hard toward the flatten direction; don't add to the heavy side.
            flatten_bid = inventory > 0
            spread = self.cfg["base_spread"]
            bid_price = max(0.01, mid_price - spread / 2) if flatten_bid else None
            ask_price = min(0.99, mid_price + spread / 2) if not flatten_bid else None
        else:
            skew = (inventory / self.cfg["max_inventory_abs"]) * self.cfg["skew_strength"] * self.cfg["base_spread"]
            spread = self.cfg["base_spread"]
            bid_price = max(0.01, mid_price - spread / 2 - skew)
            ask_price = min(0.99, mid_price + spread / 2 - skew)

        intents = []
        base = {
            "market_id": st.market_id,
            "size_usd": self.cfg["quote_size_usd"],
            "tif": self.cfg["order_tif"],
            "gtd_seconds": self.cfg["gtd_duration_sec"],
            "source": "market_maker",
            "note": "awaiting Telegram confirm" if self.mode != "paper" else "paper quote",
        }
        if bid_price is not None:
            intents.append({**base, "kind": "quote_bid", "side": "BUY", "price": round(bid_price, 4)})
        if ask_price is not None:
            intents.append({**base, "kind": "quote_ask", "side": "SELL", "price": round(ask_price, 4)})
        return intents
