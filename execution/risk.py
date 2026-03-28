"""
Risk Manager -- Circuit breakers, position limits, drawdown stops.
Protects capital by enforcing hard limits on losses and exposure.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


class RiskManager:
    """Enforces risk limits and circuit breakers."""

    def __init__(self, config: dict):
        self.max_daily_loss = config.get("max_daily_loss_usd", 20.0)
        self.max_open = config.get("max_open_positions", 5)
        self.max_consecutive_losses = config.get("max_consecutive_losses", 3)
        self.max_portfolio_pct = config.get("max_portfolio_pct", 0.10)
        self.cooldown_min = config.get("cooldown_after_stop_min", 60)

        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.open_positions = 0
        self.halted = False
        self.halt_reason = ""
        self.halt_until = None
        self.today = datetime.now(timezone.utc).date()

    def _reset_daily(self):
        """Reset daily counters at midnight UTC."""
        now = datetime.now(timezone.utc).date()
        if now != self.today:
            logger.info(f"New day -- resetting daily P&L (was ${self.daily_pnl:.2f})")
            self.daily_pnl = 0.0
            self.today = now
            # Don't auto-unhalt -- manual or cooldown-based

    def can_trade(self) -> tuple:
        """
        Check if trading is allowed.
        Returns (allowed: bool, reason: str)
        """
        self._reset_daily()

        # Check cooldown
        if self.halted and self.halt_until:
            if datetime.now(timezone.utc) < self.halt_until:
                remaining = (self.halt_until - datetime.now(timezone.utc)).seconds // 60
                return False, f"Halted: {self.halt_reason} ({remaining}m remaining)"
            else:
                logger.info("Cooldown expired -- resuming trading")
                self.halted = False
                self.halt_reason = ""
                self.halt_until = None

        if self.halted:
            return False, f"Halted: {self.halt_reason}"

        # Daily loss limit
        if self.daily_pnl <= -self.max_daily_loss:
            self._halt(f"Daily loss limit (${self.max_daily_loss}) reached")
            return False, self.halt_reason

        # Consecutive losses
        if self.consecutive_losses >= self.max_consecutive_losses:
            self._halt(f"{self.max_consecutive_losses} consecutive losses")
            return False, self.halt_reason

        # Max open positions
        if self.open_positions >= self.max_open:
            return False, f"Max open positions ({self.max_open}) reached"

        return True, "OK"

    def check_position_size(self, size_usd: float, bankroll: float) -> tuple:
        """
        Validate a proposed position size.
        Returns (allowed: bool, reason: str)
        """
        if size_usd <= 0:
            return False, "Size must be > 0"

        if bankroll > 0 and size_usd / bankroll > self.max_portfolio_pct:
            return False, f"Size ${size_usd:.2f} exceeds {self.max_portfolio_pct*100:.0f}% of bankroll"

        return True, "OK"

    def record_trade_result(self, pnl: float):
        """Record a trade's P&L and update risk state."""
        self.daily_pnl += pnl

        if pnl < 0:
            self.consecutive_losses += 1
            logger.warning(
                f"Loss: ${pnl:.2f} | Daily P&L: ${self.daily_pnl:.2f} | "
                f"Consecutive losses: {self.consecutive_losses}"
            )
        else:
            self.consecutive_losses = 0
            logger.info(f"Win: ${pnl:.2f} | Daily P&L: ${self.daily_pnl:.2f}")

    def position_opened(self):
        """Track position count."""
        self.open_positions += 1

    def position_closed(self):
        """Track position count."""
        self.open_positions = max(0, self.open_positions - 1)

    def _halt(self, reason: str):
        """Activate circuit breaker."""
        self.halted = True
        self.halt_reason = reason
        self.halt_until = datetime.now(timezone.utc) + timedelta(minutes=self.cooldown_min)
        logger.critical(f"CIRCUIT BREAKER: {reason} -- halted for {self.cooldown_min}m")

    def manual_resume(self):
        """Manually resume trading after halt."""
        self.halted = False
        self.halt_reason = ""
        self.halt_until = None
        self.consecutive_losses = 0
        logger.info("Trading manually resumed")

    def status(self) -> dict:
        """Get current risk state."""
        can, reason = self.can_trade()
        return {
            "can_trade": can,
            "reason": reason,
            "daily_pnl": round(self.daily_pnl, 2),
            "consecutive_losses": self.consecutive_losses,
            "open_positions": self.open_positions,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
        }
