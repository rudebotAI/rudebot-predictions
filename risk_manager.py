"""
Risk Manager for PredictionBot v4.1
Persistent risk state machine — survives restarts.

Features:
- Daily/total loss tracking with auto-halt
- Consecutive loss circuit breaker
- Position count enforcement
- Cooldown timer after circuit break
- State persisted to risk_state.json
- Telegram integration for alerts
"""

import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

STATE_FILE = "logs/risk_state.json"


class RiskManager:
    """
    Enforces risk limits before every trade entry.
    Persists state to disk so limits survive bot restarts.
    """

    def __init__(self, config):
        self.config = config.risk
        self.mode = config.mode

        # State — loaded from disk or initialized
        self._daily_loss: float = 0.0
        self._total_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._open_positions: Dict[str, dict] = {}
        self._halted: bool = False
        self._halt_reason: str = ""
        self._halt_time: Optional[str] = None
        self._last_reset_date: str = ""
        self._trade_count: int = 0
        self._win_count: int = 0

        self._load_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self):
        """Load risk state from disk."""
        path = Path(STATE_FILE)
        if not path.exists():
            logger.info("No risk state file — starting fresh")
            return

        try:
            with open(path) as f:
                state = json.load(f)

            self._daily_loss = state.get("daily_loss", 0.0)
            self._total_pnl = state.get("total_pnl", 0.0)
            self._consecutive_losses = state.get("consecutive_losses", 0)
            self._open_positions = state.get("open_positions", {})
            self._halted = state.get("halted", False)
            self._halt_reason = state.get("halt_reason", "")
            self._halt_time = state.get("halt_time")
            self._last_reset_date = state.get("last_reset_date", "")
            self._trade_count = state.get("trade_count", 0)
            self._win_count = state.get("win_count", 0)

            logger.info(f"Risk state loaded: daily_loss=${self._daily_loss:.2f}, "
                        f"positions={len(self._open_positions)}, "
                        f"halted={self._halted}")
        except Exception as e:
            logger.error(f"Failed to load risk state: {e}")

    def _save_state(self):
        """Persist risk state to disk."""
        state = {
            "daily_loss": round(self._daily_loss, 2),
            "total_pnl": round(self._total_pnl, 2),
            "consecutive_losses": self._consecutive_losses,
            "open_positions": self._open_positions,
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "halt_time": self._halt_time,
            "last_reset_date": self._last_reset_date,
            "trade_count": self._trade_count,
            "win_count": self._win_count,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        path = Path(STATE_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(path, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save risk state: {e}")

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------

    def check_daily_reset(self):
        """Reset daily counters if new trading day."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._last_reset_date:
            logger.info(f"Daily reset: previous loss=${self._daily_loss:.2f}")
            self._daily_loss = 0.0
            self._last_reset_date = today

            # Clear cooldown halt if it was from yesterday
            if self._halted and "cooldown" not in self._halt_reason.lower():
                self._halted = False
                self._halt_reason = ""
                self._halt_time = None

            self._save_state()

    # ------------------------------------------------------------------
    # Pre-trade gate
    # ------------------------------------------------------------------

    def can_trade(self, market_id: str, position_usd: float,
                  side: str = "yes") -> Tuple[bool, str]:
        """
        Check if a trade is allowed. Returns (allowed, reason).
        Must be called BEFORE every trade entry.
        """
        self.check_daily_reset()

        # 1. Halt check (circuit breaker or cooldown)
        if self._halted:
            # Check cooldown expiry
            if self._halt_time:
                halt_dt = datetime.fromisoformat(self._halt_time)
                elapsed = (datetime.now(timezone.utc) - halt_dt).total_seconds()
                if elapsed >= self.config.cooldown_seconds:
                    logger.info("Cooldown expired — resuming trading")
                    self._halted = False
                    self._halt_reason = ""
                    self._halt_time = None
                    self._save_state()
                else:
                    remaining = self.config.cooldown_seconds - elapsed
                    return False, f"HALTED: {self._halt_reason} ({remaining:.0f}s remaining)"
            else:
                return False, f"HALTED: {self._halt_reason}"

        # 2. Daily loss limit
        if abs(self._daily_loss) >= self.config.max_daily_loss_usd:
            self._trigger_halt(f"Daily loss ${self._daily_loss:.2f} >= ${self.config.max_daily_loss_usd}")
            return False, self._halt_reason

        # 3. Consecutive loss limit
        if self._consecutive_losses >= self.config.max_consecutive_losses:
            self._trigger_halt(f"{self._consecutive_losses} consecutive losses")
            return False, self._halt_reason

        # 4. Max open positions
        if len(self._open_positions) >= self.config.max_open_positions:
            return False, f"Max positions: {len(self._open_positions)}/{self.config.max_open_positions}"

        # 5. Position size limit
        if position_usd > self.config.max_position_usd:
            return False, f"Position ${position_usd:.2f} > max ${self.config.max_position_usd}"

        # 6. Duplicate position check
        if market_id in self._open_positions:
            return False, f"Already in {market_id}"

        # 7. Remaining daily budget
        remaining_budget = self.config.max_daily_loss_usd - abs(self._daily_loss)
        if position_usd > remaining_budget * 2:  # Conservative: don't risk more than 2x remaining
            return False, f"Position ${position_usd:.2f} too large for remaining budget ${remaining_budget:.2f}"

        return True, "OK"

    # ------------------------------------------------------------------
    # Trade recording
    # ------------------------------------------------------------------

    def record_entry(self, market_id: str, entry_price: float,
                     shares: int, side: str, position_usd: float):
        """Record a new position entry."""
        self._open_positions[market_id] = {
            "entry_price": entry_price,
            "shares": shares,
            "side": side,
            "position_usd": position_usd,
            "entered_at": datetime.now(timezone.utc).isoformat(),
        }
        self._trade_count += 1
        self._save_state()
        logger.info(f"Risk: Recorded entry {market_id} ({side} {shares}@{entry_price})")

    def record_exit(self, market_id: str, pnl: float):
        """Record a position exit and update risk counters."""
        self._open_positions.pop(market_id, None)
        self._daily_loss += min(0, pnl)  # Only track losses
        self._total_pnl += pnl

        if pnl >= 0:
            self._consecutive_losses = 0
            self._win_count += 1
        else:
            self._consecutive_losses += 1
            logger.warning(f"Risk: Loss on {market_id}: ${pnl:.2f} "
                           f"(consecutive: {self._consecutive_losses})")

        self._save_state()

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _trigger_halt(self, reason: str):
        """Trigger trading halt with cooldown."""
        self._halted = True
        self._halt_reason = reason
        self._halt_time = datetime.now(timezone.utc).isoformat()
        self._save_state()
        logger.critical(f"RISK HALT: {reason}")

    def force_resume(self) -> str:
        """Manually resume trading (e.g., from Telegram /resume command)."""
        if not self._halted:
            return "Not halted"

        old_reason = self._halt_reason
        self._halted = False
        self._halt_reason = ""
        self._halt_time = None
        self._consecutive_losses = 0
        self._save_state()
        logger.info(f"Trading manually resumed (was: {old_reason})")
        return f"Resumed (was: {old_reason})"

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Get current risk status for dashboards/Telegram."""
        self.check_daily_reset()

        win_rate = self._win_count / self._trade_count if self._trade_count > 0 else 0

        return {
            "mode": self.mode,
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "daily_loss": round(self._daily_loss, 2),
            "total_pnl": round(self._total_pnl, 2),
            "open_positions": len(self._open_positions),
            "consecutive_losses": self._consecutive_losses,
            "trade_count": self._trade_count,
            "win_rate": f"{win_rate:.1%}",
            "max_daily_loss": self.config.max_daily_loss_usd,
            "max_positions": self.config.max_open_positions,
        }
