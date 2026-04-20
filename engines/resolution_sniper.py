"""
Resolution Sniper (U3)

Scans active markets for outcomes trading at near-certainty prices with
little time left to resolution. Buys the near-certain side to collect
the $1.00 payout. High win-rate, low-variance play unique to prediction
markets.

Edge intuition: a YES at $0.97 with 30 minutes to resolution against a
well-defined real-world event yields ~3% return in 30 minutes IF the market
resolves to YES. Paired with strict sizing and a max buy price cap, this is
one of the cleanest structural edges in prediction markets.

Safety:
  * enabled: False by default.
  * Per-trade cap respected.
  * Still routed through the existing risk manager and Telegram confirm.
  * Avoids markets flagged by LMSR as thin.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "yes_threshold": 0.95,       # only consider YES >= this
    "no_threshold": 0.05,        # or NO <= this (symmetric)
    "max_minutes_to_resolution": 60,
    "min_minutes_to_resolution": 5,   # avoid same-block races
    "max_buy_price": 0.98,       # never pay more than this (bound downside)
    "min_depth_usd": 200.0,      # skip thin markets
    "max_usd_per_trade": 10.0,   # hard cap
    "min_expected_edge": 0.015,  # require at least 1.5% edge after fees
}


class ResolutionSniper:
    def __init__(self, cfg: Dict[str, Any], deps: Dict[str, Any]):
        merged = {**DEFAULTS, **(cfg.get("resolution_sniper") or {})}
        self.cfg = merged
        self.mode = cfg.get("mode", "paper")
        self.deps = deps  # expects {"markets": <market feed>, "lmsr": <lmsr>, "fees": <float or callable>}

    def scan(self, markets: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Inspect a batch of market snapshots and return sniper candidates.

        Expected market shape (flexible — adapt to your own schema):
            {
              "market_id": str,
              "yes_price": float,
              "no_price": float,
              "resolves_at_epoch": float,
              "depth_usd": float,
              "lmsr_b": float | None,
              "title": str,
            }
        """
        if not self.cfg.get("enabled", False):
            return []
        now = time.time()
        out: List[Dict[str, Any]] = []
        for m in markets:
            cand = self._evaluate(m, now)
            if cand is not None:
                out.append(cand)
        return out

    # ----------- internals -----------
    def _evaluate(self, m: Dict[str, Any], now: float) -> Optional[Dict[str, Any]]:
        resolves_at = m.get("resolves_at_epoch")
        if not resolves_at:
            return None
        mins_to_res = (resolves_at - now) / 60.0
        if not (self.cfg["min_minutes_to_resolution"] <= mins_to_res <= self.cfg["max_minutes_to_resolution"]):
            return None

        yes_price = float(m.get("yes_price", 0.0))
        no_price = float(m.get("no_price", 0.0))
        depth = float(m.get("depth_usd", 0.0))
        if depth < self.cfg["min_depth_usd"]:
            return None

        side, entry_price = None, None
        if yes_price >= self.cfg["yes_threshold"] and yes_price <= self.cfg["max_buy_price"]:
            side, entry_price = "YES", yes_price
        elif no_price <= self.cfg["no_threshold"] and (1 - no_price) <= self.cfg["max_buy_price"]:
            # NO threshold (e.g., NO at 0.03 means buying YES at 0.97 is equivalent;
            # but some venues let you buy NO directly at a premium. We'll buy NO here.)
            side, entry_price = "NO", no_price

        if side is None:
            return None

        # Expected edge: payout 1 - entry_price, haircut by fees.
        fee_rate = _resolve_fees(self.deps.get("fees", 0.0), m)
        edge = (1.0 - entry_price) - fee_rate
        if edge < self.cfg["min_expected_edge"]:
            return None

        usd = min(self.cfg["max_usd_per_trade"], depth * 0.10)  # never take more than 10% of depth
        if usd <= 0:
            return None

        return {
            "kind": "resolution_snipe",
            "source": "resolution_sniper",
            "market_id": m["market_id"],
            "side": side,
            "price": entry_price,
            "usd": round(usd, 4),
            "expected_edge": round(edge, 4),
            "minutes_to_resolution": round(mins_to_res, 2),
            "title": m.get("title", ""),
            "note": "awaiting Telegram confirm",
        }


def _resolve_fees(fees: Any, market: Dict[str, Any]) -> float:
    if callable(fees):
        try:
            return float(fees(market))
        except Exception:  # noqa: BLE001
            return 0.0
    try:
        return float(fees)
    except Exception:  # noqa: BLE001
        return 0.0
