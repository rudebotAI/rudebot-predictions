"""
Order Types + Depth Guard (U4 + U5)

Provides first-class FAK (Fill-or-Kill) and GTD (Good-Till-Date) order
primitives for the execution layer, plus a hard orderbook depth guard
that runs before every order to prevent partial fills into thin books.

Safety:
  * This module defines DATA TYPES and a DEPTH-GUARD predicate only.
    It does not place real orders.
  * Placement is done by execution.paper.PaperEngine (simulated) or
    execution.live.LiveEngine (real, opt-in). This module is shared
    plumbing used by both.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


class OrderTIF(str, Enum):
    FAK = "FAK"          # Fill-or-Kill: execute immediately what you can, cancel remainder
    GTD = "GTD"          # Good-Till-Date: rest on book until expiry
    IOC = "IOC"          # Immediate-or-Cancel (alias for FAK in some venues)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class OrderIntent:
    market_id: str
    outcome: str                     # "YES" or "NO"
    side: Side
    price: float
    size_usd: float
    tif: OrderTIF = OrderTIF.FAK
    gtd_expiry_epoch: Optional[float] = None
    client_order_id: str = ""        # idempotency key
    source: str = "unknown"          # which engine emitted this
    note: str = ""

    def __post_init__(self):
        if self.tif == OrderTIF.GTD and self.gtd_expiry_epoch is None:
            # default: 60 seconds
            self.gtd_expiry_epoch = time.time() + 60


# --- Depth Guard --------------------------------------------------------------

DEFAULTS: Dict[str, Any] = {
    "min_depth_usd": 200.0,       # must have at least this much resting at the price level
    "max_price_slippage": 0.01,   # 1¢ max slippage acceptable
}


class DepthGuardError(Exception):
    """Raised when an order would fill into insufficient depth."""


def check_depth(
    intent: OrderIntent,
    book: Dict[str, Any],
    cfg: Optional[Dict[str, Any]] = None,
) -> None:
    """Validate book liquidity before placing `intent`. Raises DepthGuardError if bad.

    Expected book shape:
        {"bids": [[price, size], ...], "asks": [[price, size], ...]}

    The guard verifies that:
      * There is at least `min_depth_usd` of resting size on the side being taken
        within the slippage tolerance of the intent's price.
      * No order can eat through more than `max_price_slippage` of price levels.
    """
    merged = {**DEFAULTS, **(cfg or {})}
    if intent.side == Side.BUY:
        levels = book.get("asks") or []
    else:
        levels = book.get("bids") or []
    if not levels:
        raise DepthGuardError(f"no {intent.side.value} side depth on book for {intent.market_id}")

    target_price = intent.price
    tolerance = merged["max_price_slippage"]
    available_usd = 0.0
    for price_str, size_str in levels:
        price = float(price_str)
        size = float(size_str)
        if intent.side == Side.BUY:
            if price > target_price + tolerance:
                break
        else:
            if price < target_price - tolerance:
                break
        available_usd += price * size

    if available_usd < merged["min_depth_usd"]:
        raise DepthGuardError(
            f"depth guard: only ${available_usd:.2f} within tolerance on {intent.market_id}; "
            f"need >= ${merged['min_depth_usd']:.2f}"
        )
    if available_usd < intent.size_usd:
        raise DepthGuardError(
            f"depth guard: order size ${intent.size_usd:.2f} exceeds available "
            f"${available_usd:.2f} within tolerance on {intent.market_id}"
        )


# --- Idempotency helpers ------------------------------------------------------

def make_client_order_id(source: str, market_id: str, salt: str = "") -> str:
    """Deterministic client order ID to prevent double-fills on retry.

    Callers should include something unique to the logical trade event
    (e.g., the whale tx_hash, or copy snapshot delta id) as `salt`.
    """
    import hashlib
    base = f"{source}|{market_id}|{salt}|{int(time.time() // 5)}"
    h = hashlib.sha1(base.encode()).hexdigest()[:16]
    return f"rudebot-{source[:6]}-{h}"
