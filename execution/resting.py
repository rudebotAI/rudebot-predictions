"""
Resting (maker) orders — "post only, never take".

Whelan et al. (2025, 46k Kalshi contracts) find takers lose ~19pp more than
makers on average; mechanically a maker pays 0 fee (maker multiplier is 0 in
nearly every series, July 2026 fee schedule) versus the taker's
0.07·P·(1−P), and captures the spread instead of paying it. For a $10 bot
that is the only edge that is essentially guaranteed, so every ENTRY is a
post-only limit order resting at our side's bid for `rest_seconds`, then
expires. Exits keep the reduce-only IOC path (getting out matters more than
the last cent).

Two back-ends behind one ledger (logs/resting_orders.json):
- paper: a resting order is FILLED only when a later poll shows the market
  would have traded through our price (our side's ask <= our price). Queue
  position is ignored in our favour only when the market crosses us, which
  makes the paper fill rate CONSERVATIVE, not optimistic.
- live: the order is a GTC post_only order with an exchange-side
  expiration; each poll reads GET /portfolio/orders/{id} and books exactly
  the reported fill_count at the reported average price.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

OPP_KEEP = ("platform", "question", "market_id", "event_ticker", "series_ticker", "category",
            "signal", "ev", "edge", "model_prob", "model_cell", "model_reason", "market_price",
            "kelly_raw", "kelly_fractional", "size_usd", "shares", "days_to_resolution", "exchange_index")


def _now() -> float:
    return time.time()


def own_leg_bid_ask(side: str, yes_bid: float, yes_ask: float) -> tuple[float, float]:
    """(bid, ask) in the outcome's own leg. NO bid = 1 - yes ask."""
    if side == "YES":
        return yes_bid, yes_ask
    bid = (1.0 - yes_ask) if yes_ask and yes_ask > 0 else 0.0
    ask = (1.0 - yes_bid) if yes_bid and yes_bid > 0 else 0.0
    return bid, ask


class RestingOrders:
    """Persistent ledger of resting entry orders (paper and live share the
    schema; `mode` on each row keeps them apart)."""

    def __init__(self, path: str = "logs/resting_orders.json"):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.orders: list[dict] = []
        self._load()

    # ---- persistence ------------------------------------------------------------------
    def _load(self):
        try:
            if self.path.exists():
                self.orders = json.loads(self.path.read_text(encoding="utf-8")) or []
        except (OSError, ValueError) as e:
            logger.warning(f"resting orders: could not load {self.path}: {e}")
            self.orders = []

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.orders, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning(f"resting orders: could not save {self.path}: {e}")

    # ---- queries -----------------------------------------------------------------------
    def active(self, mode: Optional[str] = None) -> list[dict]:
        with self._lock:
            return [o for o in self.orders if o.get("status") == "resting"
                    and (mode is None or o.get("mode") == mode)]

    def has_market(self, market_id: str, mode: str) -> bool:
        return any(o["market_id"] == market_id for o in self.active(mode))

    def recent(self, n: int = 50) -> list[dict]:
        with self._lock:
            return list(self.orders[-n:])[::-1]

    # ---- lifecycle ----------------------------------------------------------------------
    def place(self, opp: dict, price: float, contracts: int, mode: str, rest_seconds: int,
              order_id: Optional[str] = None, client_order_id: Optional[str] = None) -> dict:
        row = {
            "id": f"r{int(_now() * 1000)}-{opp.get('market_id', '')[-8:]}",
            "mode": mode, "status": "resting",
            "market_id": opp.get("market_id", ""), "event_ticker": opp.get("event_ticker", ""),
            "signal": str(opp.get("signal", "YES")).upper(),
            "price": round(float(price), 4), "contracts": int(contracts),
            "size_usd": round(float(price) * int(contracts), 4),
            "placed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "placed_ts": _now(), "expires_ts": _now() + int(rest_seconds),
            "order_id": order_id, "client_order_id": client_order_id,
            "filled": 0.0, "avg_fill": None, "fee_per_contract": 0.0,
            "opp": {k: opp.get(k) for k in OPP_KEEP if k in opp},
        }
        with self._lock:
            self.orders.append(row)
            self.orders = self.orders[-500:]
            self._save()
        logger.info(f"[{mode.upper()}] resting {row['signal']} {contracts}x @ {price:.4f} on "
                    f"{row['market_id']} for {rest_seconds}s")
        return row

    def settle(self, row: dict, status: str, filled: float = 0.0, avg_fill: Optional[float] = None,
               fee_per_contract: float = 0.0, note: str = "") -> dict:
        with self._lock:
            row["status"] = status
            row["filled"] = float(filled)
            row["avg_fill"] = avg_fill
            row["fee_per_contract"] = float(fee_per_contract or 0.0)
            row["settled_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if note:
                row["note"] = note
            self._save()
        return row

    # ---- paper fill model ----------------------------------------------------------------
    @staticmethod
    def paper_would_fill(row: dict, yes_bid: float, yes_ask: float) -> bool:
        """Conservative: filled only if our side's ASK is at or below our resting
        price, i.e. the market traded through us. Joining the bid and waiting
        for a seller to hit it is NOT counted (queue position unknown)."""
        _, ask = own_leg_bid_ask(row["signal"], yes_bid or 0.0, yes_ask or 0.0)
        return bool(ask) and 0.0 < ask <= float(row["price"]) + 1e-9

    def fill_for_book(self, row: dict) -> dict:
        """The `fill` dict execution/paper.PaperTrader.open_position expects."""
        px = float(row.get("avg_fill") or row["price"])
        n = float(row.get("filled") or 0)
        return {"price": px, "contracts": n, "size_usd": round(px * n, 4),
                "order_id": row.get("order_id") or row["id"],
                "fee_per_contract": float(row.get("fee_per_contract") or 0.0)}

    def summary(self) -> dict:
        with self._lock:
            done = [o for o in self.orders if o.get("status") in ("filled", "partial", "expired", "cancelled")]
            filled = [o for o in done if o.get("status") in ("filled", "partial")]
            return {"resting": len(self.active()), "settled": len(done), "filled": len(filled),
                    "fill_rate": round(len(filled) / len(done), 3) if done else None}
