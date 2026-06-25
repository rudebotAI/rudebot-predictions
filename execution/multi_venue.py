"""
Multi-venue execution router.

Lets ONE bot place trades on Kalshi AND Polymarket US through a single
execute() call, dispatching by opportunity["platform"]. Keeps the proven
Kalshi path (execution/live.py LiveTrader) untouched and adds a parallel,
hard-gated Polymarket path.

Safety: the Polymarket leg only acts when the PolymarketTrader is armed
(POLYMARKET_LIVE=1 + Ed25519 creds). If a Polymarket opportunity arrives and
Polymarket is not armed, it is refused -- never silently routed elsewhere.
Every trade still passes the shared (venue-agnostic) RiskManager and, upstream
in main.py, requires Telegram confirmation.
"""
import logging

logger = logging.getLogger(__name__)

class PolymarketLiveTrader:
    """Mirror of execution/live.py LiveTrader for the Polymarket US venue."""

    def __init__(self, polymarket_trader, risk_manager):
        self.pm = polymarket_trader
        self.risk = risk_manager
        self.enabled = False

    def enable(self):
        # Only truly enables if the connector is armed with real creds.
        if self.pm.has_trading_auth():
            self.enabled = True
            logger.critical("POLYMARKET LIVE TRADING ENABLED -- real money at risk")
        else:
            logger.warning("Polymarket live requested but not armed (no creds / POLYMARKET_LIVE unset) -- scan only")

    def execute(self, opportunity: dict, size_usd: float) -> dict:
        if not (self.enabled and self.pm.has_trading_auth()):
            return {"error": "Polymarket live trading not enabled/armed"}
        market_id = opportunity.get("market_id", "")
        outcome_id = opportunity.get("outcome_id", "")
        if not market_id or not outcome_id:
            return {"error": "Polymarket order missing market_id/outcome_id"}
        side = "buy"  # we only ever BUY a YES/NO share to open
        price = opportunity.get("market_price", 0) or opportunity.get("yes_price", 0)
        if not price or price <= 0:
            return {"error": "Invalid/zero price -- refusing to size order"}

        allowed, reason = self.risk.can_trade(market_id, size_usd, opportunity.get("signal", "yes").lower())
        if not allowed:
            logger.warning(f"Risk manager blocked Polymarket trade: {reason}")
            return {"error": f"Risk blocked: {reason}"}

        result = self.pm.place_order(
            market_id=market_id, outcome_id=outcome_id, side=side,
            price=price, size_usd=size_usd,
        )
        if isinstance(result, dict) and not result.get("error"):
            shares = int(size_usd / price) if price > 0 else 0
            self.risk.record_entry(market_id, price, shares, opportunity.get("signal", "yes").lower(), size_usd)
            logger.info(f"[LIVE] Polymarket order placed: {market_id} ${size_usd:.2f} @ {price:.3f}")
            return {"success": True, "platform": "polymarket", "market_id": market_id,
                    "price": price, "size_usd": size_usd, "result": result}
        err = result.get("error") if isinstance(result, dict) else "Polymarket order failed"
        return {"error": err or "Polymarket order failed"}


class MultiVenueExecutor:
    """Routes execute() to the venue named on the opportunity. Drop-in for the
    single LiveTrader main.py used before (same execute(opp, size) signature)."""

    def __init__(self, kalshi_trader, polymarket_trader=None):
        self._venues = {"kalshi": kalshi_trader}
        if polymarket_trader is not None:
            self._venues["polymarket"] = polymarket_trader

    def enable(self):
        for v in self._venues.values():
            try:
                v.enable()
            except Exception as e:
                logger.error(f"venue enable failed: {e}")

    def execute(self, opportunity: dict, size_usd: float) -> dict:
        platform = (opportunity.get("platform") or "kalshi").lower()
        trader = self._venues.get(platform)
        if trader is None:
            return {"error": f"No executor for venue {platform!r}"}
        return trader.execute(opportunity, size_usd)
