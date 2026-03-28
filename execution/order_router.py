"""
Order Router -- Execution abstraction layer.
Matches the diagram: validated order -> Order Router -> fill receipt -> Trade log.

Routes orders through:
1. Risk manager validation
2. Platform selection (Polymarket or Kalshi)
3. Paper or live execution
4. State store update
5. Alert hook notification
"""

import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class OrderRouter:
    """
    Central execution layer. All trades flow through here.
    Enforces risk checks, routes to correct platform, logs results.
    """

    def __init__(self, risk_manager, paper_trader, live_trader, state_store, alerts, mode: str = "paper"):
        self.risk = risk_manager
        self.paper = paper_trader
        self.live = live_trader
        self.state = state_store
        self.alerts = alerts
        self.mode = mode
        self.order_count = 0

    def route_order(self, opportunity: dict, size_usd: float) -> dict:
        """
        Route an order through the full execution pipeline.

        Returns:
            dict with keys: success, order_id, fill_price, platform, mode, error
        """
        self.order_count += 1
        order_id = f"ORD-{self.order_count:04d}-{int(time.time()) % 10000}"
        platform = opportunity.get("platform", "polymarket")

        result = {
            "order_id": order_id,
            "success": False,
            "platform": platform,
            "mode": self.mode,
            "size_usd": size_usd,
            "signal": opportunity.get("signal", "?"),
            "question": opportunity.get("question", "")[:80],
            "error": None,
        }

        # ── Step 1: Risk validation ──
        can_trade, reason = self.risk.can_trade()
        if not can_trade:
            result["error"] = f"Risk blocked: {reason}"
            logger.warning(f"[{order_id}] {result['error']}")
            return result

        size_ok, size_reason = self.risk.check_position_size(size_usd, self.state.get_wallet_balance() or 50)
        if not size_ok:
            result["error"] = f"Size rejected: {size_reason}"
            logger.warning(f"[{order_id}] {result['error']}")
            return result

        # ── Step 2: Execute (paper or live) ──
        try:
            if self.mode == "paper":
                trade = self.paper.open_position(opportunity, size_usd)
                if trade:
                    result["success"] = True
                    result["fill_price"] = trade.get("entry_price", 0)
                    result["trade_id"] = trade.get("id", "")
                    self.risk.position_opened()

                    # Update state store
                    self.state.add_position(trade)

                    # Notify
                    if self.alerts.is_configured():
                        self.alerts.send_trade_opened(trade)

                    logger.info(
                        f"[{order_id}] PAPER FILL: {opportunity.get('signal')} "
                        f"{opportunity.get('question','')[:40]} @ "
                        f"{trade.get('entry_price', 0):.3f} | ${size_usd:.2f}"
                    )
                else:
                    result["error"] = "Paper trader returned None"

            elif self.mode == "live":
                if not self.live.is_enabled():
                    result["error"] = "Live trading not enabled"
                    logger.error(f"[{order_id}] {result['error']}")
                    return result

                fill = self.live.execute(opportunity, size_usd)
                if fill and not fill.get("error"):
                    result["success"] = True
                    result["fill_price"] = fill.get("price", 0)
                    result["trade_id"] = fill.get("order_id", "")
                    self.risk.position_opened()

                    self.state.add_position({
                        **opportunity,
                        "size_usd": size_usd,
                        "entry_price": fill.get("price", 0),
                        "order_id": fill.get("order_id", ""),
                        "mode": "live",
                    })

                    if self.alerts.is_configured():
                        self.alerts.send(
                            f"<b>LIVE FILL</b>\n"
                            f"{opportunity.get('signal')} {opportunity.get('question','')[:60]}\n"
                            f"@ {fill.get('price', 0):.3f} | ${size_usd:.2f}"
                        )

                    logger.info(f"[{order_id}] LIVE FILL: ${size_usd:.2f}")
                else:
                    result["error"] = fill.get("error", "Live execution failed")

        except Exception as e:
            result["error"] = str(e)
            logger.error(f"[{order_id}] Execution error: {e}")
            self.state.log_error(f"Order {order_id}: {str(e)[:100]}")

        return result

    def close_position(self, position_id: str, exit_price: float, reason: str = "") -> Optional[dict]:
        """
        Close an open position through the router.
        Handles P&L calculation, risk updates, state store, and notifications.
        """
        # Close in paper trader
        closed = self.paper.close_position(position_id, exit_price, reason)
        if not closed:
            return None

        pnl = closed.get("pnl", 0)

        # Update risk manager
        self.risk.position_closed()
        self.risk.record_trade_result(pnl)

        # Update state store
        self.state.remove_position(position_id)
        self.state.record_pnl(pnl, pnl >= 0)

        # Notify
        if self.alerts.is_configured():
            self.alerts.send_trade_closed(closed)

        logger.info(
            f"Position closed: {position_id} | "
            f"P&L: ${pnl:.2f} | Reason: {reason}"
        )

        return closed

    def get_status(self) -> dict:
        """Return router status for dashboard/monitoring."""
        return {
            "mode": self.mode,
            "orders_processed": self.order_count,
            "open_positions": len(self.state.get_positions()),
            "state": self.state.get_full_state(),
        }
