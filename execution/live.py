"""
Live Trading Engine -- Executes real trades via platform APIs.
ONLY activates when config mode = "live" AND user confirms via Telegram.
"""

import logging

logger = logging.getLogger(__name__)


class LiveTrader:
    """
    Real money trade execution.
    Wraps platform connectors with additional safety checks.
    """

    def __init__(self, polymarket_connector, kalshi_connector, risk_manager):
        self.poly = polymarket_connector
        self.kalshi = kalshi_connector
        self.risk = risk_manager
        self.enabled = False  # Must be explicitly enabled

    def enable(self):
        """Enable live trading (called after config check)."""
        self.enabled = True
        logger.critical("LIVE TRADING ENABLED -- real money at risk")

    def execute(self, opportunity: dict, size_usd: float) -> dict:
        """
        Execute a real trade on the appropriate platform.

        Returns trade result dict or error.
        """
        if not self.enabled:
            logger.error("Live trading not enabled -- ignoring execute call")
            return {"error": "Live trading not enabled"}

        # Final risk check
        can_trade, reason = self.risk.can_trade()
        if not can_trade:
            logger.warning(f"Risk manager blocked trade: {reason}")
            return {"error": f"Risk blocked: {reason}"}

        platform = opportunity.get("platform", "")
        price = opportunity.get("market_price", 0)
        signal = opportunity.get("signal", "YES")

        if platform == "polymarket":
            return self._execute_polymarket(opportunity, size_usd, price, signal)
        elif platform == "kalshi":
            return self._execute_kalshi(opportunity, size_usd, price, signal)
        else:
            return {"error": f"Unknown platform: {platform}"}

    def _execute_polymarket(self, opp: dict, size_usd: float, price: float, signal: str) -> dict:
        """Place order on Polymarket."""
        token_ids = opp.get("token_ids", [])
        if not token_ids:
            return {"error": "No token IDs for Polymarket order"}

        # YES = buy token[0], NO = buy token[1]
        token_id = token_ids[0] if signal == "YES" else (token_ids[1] if len(token_ids) > 1 else None)
        if not token_id:
            return {"error": "Missing token ID"}

        shares = size_usd / price if price > 0 else 0

        result = self.poly.place_order(
            token_id=token_id,
            side="buy",
            price=price,
            size=shares,
        )

        if result:
            self.risk.position_opened()
            logger.info(f"[LIVE] Polymarket order placed: {signal} @ {price} | ${size_usd}")
            return {"success": True, "platform": "polymarket", "result": result}

        return {"error": "Polymarket order failed"}

    def _execute_kalshi(self, opp: dict, size_usd: float, price: float, signal: str) -> dict:
        """Place order on Kalshi."""
        market_id = opp.get("market_id", "")
        if not market_id:
            return {"error": "No market ID for Kalshi order"}

        price_cents = int(price * 100)
        contracts = max(1, int(size_usd / price)) if price > 0 else 0

        result = self.kalshi.place_order(
            market_id=market_id,
            side=signal.lower(),
            price_cents=price_cents,
            count=contracts,
        )

        if result:
            self.risk.position_opened()
            logger.info(f"[LIVE] Kalshi order placed: {signal} @ {price_cents}c | {contracts} contracts")
            return {"success": True, "platform": "kalshi", "result": result}

        return {"error": "Kalshi order failed"}
