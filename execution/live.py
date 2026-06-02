"""
Live Trading Engine -- Executes real trades on Kalshi.
ONLY activates when config mode = "live", live auth is configured, AND the
user confirms each trade via Telegram (enforced upstream in main.py).

Kalshi-only. Polymarket support was removed from the project.
"""

import logging

logger = logging.getLogger(__name__)


class LiveTrader:
    """
    Real-money trade execution. Wraps the Kalshi connector with a final
    risk-manager check. Must be explicitly enabled via enable().
    """

    def __init__(self, kalshi_connector, risk_manager):
        self.kalshi = kalshi_connector
        self.risk = risk_manager
        self.enabled = False  # Must be explicitly enabled after preflight checks

    def enable(self):
        """Enable live trading (called only after main.py preflight passes)."""
        self.enabled = True
        logger.critical("LIVE TRADING ENABLED -- real money at risk")

    def execute(self, opportunity: dict, size_usd: float) -> dict:
        """
        Execute a real trade on Kalshi. Returns a result dict.
        On success: {"success": True, "platform": "kalshi", "result": ..., ...}
        On failure/block: {"error": "..."}.
        """
        if not self.enabled:
            logger.error("Live trading not enabled -- ignoring execute call")
            return {"error": "Live trading not enabled"}

        market_id = opportunity.get("market_id", "")
        if not market_id:
            return {"error": "No market ID for Kalshi order"}

        signal = str(opportunity.get("signal", "YES")).lower()
        price = opportunity.get("market_price", 0) or opportunity.get("yes_price", 0)
        if not price or price <= 0:
            return {"error": "Invalid/zero price -- refusing to size order"}

        # Final risk check (current risk_manager signature: market_id, position_usd, side)
        allowed, reason = self.risk.can_trade(market_id, size_usd, signal)
        if not allowed:
            logger.warning(f"Risk manager blocked trade: {reason}")
            return {"error": f"Risk blocked: {reason}"}

        price_cents = int(round(price * 100))
        contracts = max(1, int(size_usd / price))

        result = self.kalshi.place_order(
            market_id=market_id,
            side=signal,
            price_cents=price_cents,
            count=contracts,
        )

        if isinstance(result, dict) and not result.get("error"):
            self.risk.record_entry(market_id, price, contracts, signal, size_usd)
            logger.info(
                f"[LIVE] Kalshi order placed: {signal.upper()} @ {price_cents}c | "
                f"{contracts} contracts | ${size_usd:.2f}"
            )
            return {
                "success": True,
                "platform": "kalshi",
                "market_id": market_id,
                "side": signal,
                "price": price,
                "contracts": contracts,
                "size_usd": size_usd,
                "result": result,
            }

        err = result.get("error") if isinstance(result, dict) else "Kalshi order failed"
        return {"error": err or "Kalshi order failed"}
