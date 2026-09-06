"""
Live Trading Engine -- Executes real trades on Kalshi (V2 order surface).

ONLY activates when config mode = "live", live auth is configured, AND the
user confirms each trade via Telegram (enforced upstream in main.py).

Execution model (v6):
- Entries are IMMEDIATE-OR-CANCEL limit orders priced at the touch plus a
  bounded slippage allowance. Whatever does not fill instantly is cancelled
  by the exchange, so the bot never leaves resting orders it isn't watching.
- Only the FILLED quantity (at the exchange-reported average fill price) is
  booked into the position book / risk manager. Zero fill = no position.
- Pre-trade guards: fresh quote, spread cap, top-of-book depth, price grid.
- Exits (`close`) are reduce-only IOC sells of the held outcome.
- Deterministic client_order_id per (market, side, scan) so a retry after a
  network timeout can be reconciled instead of double-filled.
"""

import hashlib
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


def make_client_order_id(market_id: str, side: str, action: str, nonce: str) -> str:
    raw = f"{market_id}|{side}|{action}|{nonce}"
    return "rb-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


class LiveTrader:
    """
    Real-money trade execution. Wraps the Kalshi connector with a final
    risk-manager check. Must be explicitly enabled via enable().
    """

    def __init__(self, kalshi_connector, risk_manager, config: Optional[dict] = None):
        cfg = config or {}
        self.kalshi = kalshi_connector
        self.risk = risk_manager
        self.enabled = False  # Must be explicitly enabled after preflight checks
        # Max acceptable spread (yes-leg dollars) before we refuse to cross.
        self.max_spread = float(cfg.get("max_spread", 0.05))
        # Extra we are willing to pay above the touch to get filled (dollars).
        self.slippage = float(cfg.get("slippage", 0.01))
        # Minimum fraction of intended size that must be available at the touch.
        self.min_depth_ratio = float(cfg.get("min_depth_ratio", 0.5))

    def enable(self):
        """Enable live trading (called only after main.py preflight passes)."""
        self.enabled = True
        logger.critical("LIVE TRADING ENABLED -- real money at risk")

    # ------------------------------------------------------------------
    def _fresh_quote(self, market_id: str) -> Optional[dict]:
        m = self.kalshi.get_market(market_id)
        if not m:
            return None
        q = self.kalshi.quote(m)
        q["market"] = m
        return q

    def _side_prices(self, side: str, q: dict) -> dict:
        """Touch prices in the outcome's own leg. For NO, the ask we lift is
        (1 - yes_bid) and the size available is the yes bid size."""
        if side == "yes":
            return {"ask": q["yes_ask"], "bid": q["yes_bid"], "ask_size": q["ask_size"],
                    "bid_size": q["bid_size"]}
        yb, ya = q["yes_bid"], q["yes_ask"]
        return {
            "ask": (1.0 - yb) if yb > 0 else 0.0,
            "bid": (1.0 - ya) if ya > 0 else 0.0,
            "ask_size": q["bid_size"],
            "bid_size": q["ask_size"],
        }

    # ------------------------------------------------------------------
    def execute(self, opportunity: dict, size_usd: float, nonce: Optional[str] = None) -> dict:
        """
        Open a real position on Kalshi. Returns a result dict.
        On success: {"success": True, "platform": "kalshi", "contracts": filled,
                     "price": avg_fill, "size_usd": actual, "order_id": ..., ...}
        On failure/block: {"error": "..."}.
        """
        if not self.enabled:
            logger.error("Live trading not enabled -- ignoring execute call")
            return {"error": "Live trading not enabled"}

        market_id = opportunity.get("market_id", "")
        if not market_id:
            return {"error": "No market ID for Kalshi order"}

        side = str(opportunity.get("signal", "YES")).lower()
        if side not in ("yes", "no"):
            return {"error": f"invalid signal {side!r}"}
        if size_usd <= 0:
            return {"error": "Non-positive size -- refusing to place order"}

        # 1. Fresh quote -- the scan may be minutes old.
        q = self._fresh_quote(market_id)
        if not q:
            return {"error": "Could not fetch fresh quote"}
        touch = self._side_prices(side, q)
        ask = touch["ask"]
        if not ask or ask <= 0 or ask >= 1:
            return {"error": f"No {side.upper()} ask available (ask={ask})"}

        # 2. Spread guard.
        if q["spread"] is None or q["spread"] > self.max_spread:
            return {"error": f"Spread {q['spread']} exceeds max {self.max_spread}"}

        # 3. Size and depth guard.
        limit_price = min(0.99, ask + self.slippage)
        contracts = int(size_usd / limit_price)
        if contracts < 1:
            return {"error": f"Size ${size_usd:.2f} buys <1 contract at {limit_price:.4f}"}
        if touch["ask_size"] and touch["ask_size"] < contracts * self.min_depth_ratio:
            contracts = max(1, int(touch["ask_size"]))
            logger.info(f"[LIVE] Depth-limited {market_id} to {contracts} contracts")

        # 4. Final risk check at the actual notional.
        notional = contracts * limit_price
        allowed, reason = self.risk.can_trade(
            market_id, notional, side, event_ticker=opportunity.get("event_ticker")
        )
        if not allowed:
            logger.warning(f"Risk manager blocked trade: {reason}")
            return {"error": f"Risk blocked: {reason}"}

        # 5. Place IOC at the touch + slippage; exchange cancels the remainder.
        coid = make_client_order_id(market_id, side, "buy", nonce or str(int(time.time())))
        result = self.kalshi.place_order(
            market_id=market_id, side=side, price=limit_price, count=contracts,
            action="buy", time_in_force="immediate_or_cancel", client_order_id=coid,
            price_ranges=q.get("price_ranges"),
        )
        if result is None:
            return {"error": "Kalshi order request failed (no response)", "client_order_id": coid}
        if result.get("error"):
            return {"error": result["error"], "client_order_id": coid}

        filled = float(result.get("fill_count") or 0)
        if filled <= 0:
            logger.warning(f"[LIVE] IOC order {result.get('order_id')} on {market_id} filled 0 contracts")
            return {"error": "No fill (IOC cancelled)", "order_id": result.get("order_id")}

        # Average fill price comes back in yes-leg dollars; convert to the side's leg.
        avg_yes = result.get("average_fill_price") or result.get("price")
        avg_price = avg_yes if side == "yes" else (1.0 - avg_yes)
        actual_usd = round(filled * avg_price, 4)

        self.risk.record_entry(market_id, avg_price, filled, side, actual_usd,
                               event_ticker=opportunity.get("event_ticker"))
        logger.info(
            f"[LIVE] Kalshi FILLED: {side.upper()} {filled:g}/{contracts} @ {avg_price:.4f} "
            f"| ${actual_usd:.2f} | order {result.get('order_id')}"
        )
        return {
            "success": True,
            "platform": "kalshi",
            "market_id": market_id,
            "side": side,
            "price": avg_price,
            "contracts": filled,
            "requested_contracts": contracts,
            "size_usd": actual_usd,
            "order_id": result.get("order_id"),
            "client_order_id": coid,
            "fee_per_contract": result.get("average_fee_paid", 0.0),
            "result": result,
        }

    # ------------------------------------------------------------------
    def close(self, position: dict, reason: str = "manual", nonce: Optional[str] = None) -> dict:
        """Sell out of a held outcome with a reduce-only IOC at the bid minus
        slippage. Returns {"success": True, "contracts": filled, "price": avg}
        in the position's own leg, or {"error": ...}."""
        if not self.enabled:
            return {"error": "Live trading not enabled"}
        market_id = position.get("market_id", "")
        side = str(position.get("signal") or position.get("side") or "yes").lower()
        contracts = float(position.get("shares") or position.get("contracts") or 0)
        if not market_id or contracts <= 0:
            return {"error": "Position missing market_id/shares"}

        q = self._fresh_quote(market_id)
        if not q:
            return {"error": "Could not fetch fresh quote"}
        touch = self._side_prices(side, q)
        bid = touch["bid"]
        if not bid or bid <= 0:
            return {"error": f"No {side.upper()} bid to sell into"}
        limit_price = max(0.01, bid - self.slippage)

        coid = make_client_order_id(market_id, side, "sell", nonce or str(int(time.time())))
        result = self.kalshi.place_order(
            market_id=market_id, side=side, price=limit_price, count=contracts,
            action="sell", time_in_force="immediate_or_cancel", client_order_id=coid,
            reduce_only=True, price_ranges=q.get("price_ranges"),
        )
        if result is None or result.get("error"):
            return {"error": (result or {}).get("error", "Kalshi sell request failed")}
        filled = float(result.get("fill_count") or 0)
        if filled <= 0:
            return {"error": "No fill on exit (IOC cancelled)", "order_id": result.get("order_id")}
        avg_yes = result.get("average_fill_price") or result.get("price")
        avg_price = avg_yes if side == "yes" else (1.0 - avg_yes)
        logger.info(
            f"[LIVE] Kalshi EXIT ({reason}): {side.upper()} {filled:g}/{contracts:g} @ {avg_price:.4f} "
            f"| order {result.get('order_id')}"
        )
        return {
            "success": True,
            "contracts": filled,
            "price": avg_price,
            "partial": filled < contracts,
            "order_id": result.get("order_id"),
            "fee_per_contract": result.get("average_fee_paid", 0.0),
        }
