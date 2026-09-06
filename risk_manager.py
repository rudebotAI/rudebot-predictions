"""
Risk Manager for PredictionBot v6
Persistent risk state machine — survives restarts.

Gates (all checked before every entry):
- Circuit-breaker halt with cooldown (auto-resumes, resets the streak that tripped it)
- Daily loss limit
- Consecutive-loss limit
- Max-drawdown breaker: equity below (1 - max_drawdown_pct) * peak => halt
  until /resume (this one does NOT auto-expire — a human has to look)
- Max open positions, per-market duplicate check, per-position cap
- Per-event exposure cap (correlated legs of the same event share a budget)
- Remaining daily budget

Also tracks: total P&L, equity peak, per-trade returns for Sharpe.
State persisted atomically to logs/risk_state.json.
"""

import json
import os
import math
import logging
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

STATE_FILE = "logs/risk_state.json"
LIVE_STATE_FILE = "logs/risk_state_live.json"
MAX_RETURNS_KEPT = 500


def atomic_write_json(path: Path, payload) -> None:
    """Write JSON via temp file + rename so a crash never leaves a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def sharpe_ratio(returns: List[float]) -> Optional[float]:
    """Per-trade Sharpe (mean / stdev of per-trade returns). None if <2 trades."""
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return mean / sd


class RiskManager:
    """
    Enforces risk limits before every trade entry.
    Persists state to disk so limits survive bot restarts.
    """

    def __init__(self, config, state_file: Optional[str] = None):
        self.config = config.risk
        self.mode = config.mode
        # Paper and live never share risk state. Resolve the module globals at
        # call time so tests can monkeypatch them.
        if state_file:
            self.state_path = Path(state_file)
        else:
            self.state_path = Path(LIVE_STATE_FILE if self.mode == "live" else STATE_FILE)
        # Optional callback(reason) invoked when a halt trips (Telegram alert).
        self.on_halt = None

        # State — loaded from disk or initialized
        self._daily_loss: float = 0.0
        self._total_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._open_positions: Dict[str, dict] = {}
        self._halted: bool = False
        self._halt_reason: str = ""
        self._halt_time: Optional[str] = None
        self._halt_sticky: bool = False   # True => no cooldown auto-resume
        self._halt_kind: str = ""         # "daily" | "streak" | "drawdown" | ""
        self._last_reset_date: str = ""
        self._trade_count: int = 0
        self._win_count: int = 0
        self._returns: List[float] = []   # per-trade return on stake
        # Live mode seeds equity from the exchange balance (set_starting_equity);
        # the configured bankroll_usd is a paper-only notion.
        self._starting_equity: float = (
            0.0 if self.mode == "live"
            else float(getattr(self.config, "bankroll_usd", 0.0) or 0.0)
        )
        self._peak_equity: float = self._starting_equity

        self._load_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self):
        """Load risk state from disk."""
        path = self.state_path
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
            self._halt_sticky = state.get("halt_sticky", False)
            self._halt_kind = state.get("halt_kind", "")
            self._last_reset_date = state.get("last_reset_date", "")
            self._trade_count = state.get("trade_count", 0)
            self._win_count = state.get("win_count", 0)
            self._returns = list(state.get("returns", []))[-MAX_RETURNS_KEPT:]
            if state.get("starting_equity"):
                self._starting_equity = float(state["starting_equity"])
            self._peak_equity = float(state.get("peak_equity", self._starting_equity) or self._starting_equity)

            logger.info(f"Risk state loaded: daily_loss=${self._daily_loss:.2f}, "
                        f"positions={len(self._open_positions)}, "
                        f"halted={self._halted}")
        except Exception as e:
            logger.error(f"Failed to load risk state: {e}")

    def _save_state(self):
        """Persist risk state to disk (atomic)."""
        state = {
            "daily_loss": round(self._daily_loss, 4),
            "total_pnl": round(self._total_pnl, 4),
            "consecutive_losses": self._consecutive_losses,
            "open_positions": self._open_positions,
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "halt_time": self._halt_time,
            "halt_sticky": self._halt_sticky,
            "halt_kind": self._halt_kind,
            "last_reset_date": self._last_reset_date,
            "trade_count": self._trade_count,
            "win_count": self._win_count,
            "returns": self._returns[-MAX_RETURNS_KEPT:],
            "starting_equity": self._starting_equity,
            "peak_equity": self._peak_equity,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            atomic_write_json(self.state_path, state)
        except Exception as e:
            logger.error(f"Failed to save risk state: {e}")

    # ------------------------------------------------------------------
    # Equity / drawdown
    # ------------------------------------------------------------------

    @property
    def equity(self) -> float:
        """Realized equity = starting bankroll + realized P&L."""
        return self._starting_equity + self._total_pnl

    def set_starting_equity(self, equity: float):
        """Seed the bankroll (e.g. from the live Kalshi balance at startup)."""
        if equity and equity > 0 and self._starting_equity <= 0:
            self._starting_equity = float(equity)
            self._peak_equity = max(self._peak_equity, self.equity)
            self._save_state()

    @property
    def drawdown_pct(self) -> float:
        if self._peak_equity <= 0:
            return 0.0
        return max(0.0, (self._peak_equity - self.equity) / self._peak_equity)

    def _check_drawdown(self):
        limit = float(getattr(self.config, "max_drawdown_pct", 0.0) or 0.0)
        if limit <= 0 or self._peak_equity <= 0:
            return
        dd = self.drawdown_pct
        if dd >= limit and not self._halted:
            self._trigger_halt(
                f"Max drawdown {dd:.1%} >= {limit:.0%} (equity ${self.equity:.2f} vs peak ${self._peak_equity:.2f})",
                sticky=True, kind="drawdown",
            )

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

            # A daily-loss halt from yesterday is cleared; drawdown halts stay.
            if self._halted and not self._halt_sticky and self._halt_kind == "daily":
                self._halted = False
                self._halt_reason = ""
                self._halt_time = None
                self._halt_kind = ""

            self._save_state()

    # ------------------------------------------------------------------
    # Pre-trade gate
    # ------------------------------------------------------------------

    def event_exposure(self, event_ticker: str) -> float:
        if not event_ticker:
            return 0.0
        return sum(
            float(p.get("position_usd", 0) or 0)
            for p in self._open_positions.values()
            if p.get("event_ticker") == event_ticker
        )

    def can_trade(self, market_id: str, position_usd: float,
                  side: str = "yes", event_ticker: Optional[str] = None) -> Tuple[bool, str]:
        """
        Check if a trade is allowed. Returns (allowed, reason).
        Must be called BEFORE every trade entry.
        """
        self.check_daily_reset()
        self._check_drawdown()

        # 1. Halt check (circuit breaker or cooldown)
        if self._halted:
            if self._halt_sticky:
                return False, f"HALTED: {self._halt_reason} (send /resume)"
            if self._halt_kind == "daily":
                # Cleared only by the UTC daily reset (or /resume) -- a cooldown
                # would just re-trip it and wipe the loss-streak counter.
                return False, f"HALTED: {self._halt_reason} (resets at 00:00 UTC)"
            if self._halt_time:
                halt_dt = datetime.fromisoformat(self._halt_time)
                elapsed = (datetime.now(timezone.utc) - halt_dt).total_seconds()
                if elapsed >= self.config.cooldown_seconds:
                    logger.info("Cooldown expired — resuming trading (loss streak reset)")
                    self._halted = False
                    self._halt_reason = ""
                    self._halt_time = None
                    self._halt_kind = ""
                    # Without this the very next can_trade() re-trips the same breaker.
                    self._consecutive_losses = 0
                    self._save_state()
                else:
                    remaining = self.config.cooldown_seconds - elapsed
                    return False, f"HALTED: {self._halt_reason} ({remaining:.0f}s remaining)"
            else:
                return False, f"HALTED: {self._halt_reason}"

        # 2. Daily loss limit
        if abs(self._daily_loss) >= self.config.max_daily_loss_usd:
            self._trigger_halt(f"Daily loss ${self._daily_loss:.2f} >= ${self.config.max_daily_loss_usd}",
                               kind="daily")
            return False, self._halt_reason

        # 3. Consecutive loss limit
        if self._consecutive_losses >= self.config.max_consecutive_losses:
            self._trigger_halt(f"{self._consecutive_losses} consecutive losses", kind="streak")
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

        # 7. Per-event exposure cap (legs of one event are highly correlated)
        cap = float(getattr(self.config, "max_event_exposure_usd", 0.0) or 0.0)
        if cap > 0 and event_ticker:
            cur = self.event_exposure(event_ticker)
            if cur + position_usd > cap:
                return False, f"Event exposure ${cur + position_usd:.2f} > cap ${cap:.2f} for {event_ticker}"

        # 8. Remaining daily budget
        remaining_budget = self.config.max_daily_loss_usd - abs(self._daily_loss)
        if position_usd > remaining_budget * 2:  # Conservative: don't risk more than 2x remaining
            return False, f"Position ${position_usd:.2f} too large for remaining budget ${remaining_budget:.2f}"

        return True, "OK"

    # ------------------------------------------------------------------
    # Trade recording
    # ------------------------------------------------------------------

    def record_entry(self, market_id: str, entry_price: float,
                     shares: float, side: str, position_usd: float,
                     event_ticker: Optional[str] = None):
        """Record a new position entry."""
        self._open_positions[market_id] = {
            "entry_price": entry_price,
            "shares": shares,
            "side": side,
            "position_usd": position_usd,
            "event_ticker": event_ticker or "",
            "entered_at": datetime.now(timezone.utc).isoformat(),
        }
        self._trade_count += 1
        self._save_state()
        logger.info(f"Risk: Recorded entry {market_id} ({side} {shares}@{entry_price})")

    def record_partial_exit(self, market_id: str, pnl: float, stake_closed: float = 0.0):
        """Book realized P&L from a partial exit; the position stays open."""
        self._daily_loss += min(0, pnl)
        self._total_pnl += pnl
        pos = self._open_positions.get(market_id)
        if pos and stake_closed:
            pos["position_usd"] = max(0.0, float(pos.get("position_usd", 0) or 0) - stake_closed)
        self._peak_equity = max(self._peak_equity, self.equity)
        self._save_state()
        self._check_drawdown()

    def record_exit(self, market_id: str, pnl: float):
        """Record a position exit and update risk counters / equity curve."""
        pos = self._open_positions.pop(market_id, None)
        self._daily_loss += min(0, pnl)  # Only track losses
        self._total_pnl += pnl

        stake = float((pos or {}).get("position_usd", 0) or 0)
        if stake > 0:
            self._returns.append(round(pnl / stake, 6))
            self._returns = self._returns[-MAX_RETURNS_KEPT:]

        if pnl >= 0:
            self._consecutive_losses = 0
            self._win_count += 1
        else:
            self._consecutive_losses += 1
            logger.warning(f"Risk: Loss on {market_id}: ${pnl:.2f} "
                           f"(consecutive: {self._consecutive_losses})")

        self._peak_equity = max(self._peak_equity, self.equity)
        self._save_state()
        self._check_drawdown()

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _trigger_halt(self, reason: str, sticky: bool = False, kind: str = ""):
        """Trigger trading halt. Streak halts auto-resume after cooldown, daily
        halts clear at the UTC reset, sticky (drawdown) halts need /resume."""
        self._halted = True
        self._halt_reason = reason
        self._halt_sticky = sticky
        self._halt_kind = kind
        self._halt_time = datetime.now(timezone.utc).isoformat()
        self._save_state()
        logger.critical(f"RISK HALT{' (sticky)' if sticky else ''}: {reason}")
        if self.on_halt:
            try:
                self.on_halt(reason)
            except Exception as e:
                logger.warning(f"on_halt callback failed: {e}")

    def force_resume(self) -> str:
        """Manually resume trading (e.g., from Telegram /resume command)."""
        if not self._halted:
            return "Not halted"

        old_reason = self._halt_reason
        self._halted = False
        self._halt_reason = ""
        self._halt_time = None
        self._halt_sticky = False
        self._halt_kind = ""
        self._consecutive_losses = 0
        # Re-base the drawdown reference so the same breaker doesn't re-trip.
        self._peak_equity = self.equity
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
        sharpe = sharpe_ratio(self._returns)

        return {
            "mode": self.mode,
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "halt_sticky": self._halt_sticky,
            "halt_kind": self._halt_kind,
            "daily_loss": round(self._daily_loss, 2),
            "daily_pnl": round(self._daily_loss, 2),
            "total_pnl": round(self._total_pnl, 2),
            "equity": round(self.equity, 2),
            "peak_equity": round(self._peak_equity, 2),
            "drawdown_pct": round(self.drawdown_pct * 100, 2),
            "max_drawdown_pct": round(float(getattr(self.config, "max_drawdown_pct", 0) or 0) * 100, 1),
            "sharpe": round(sharpe, 3) if sharpe is not None else None,
            "open_positions": len(self._open_positions),
            "consecutive_losses": self._consecutive_losses,
            "trade_count": self._trade_count,
            "closed_trades": len(self._returns),
            "win_rate": f"{win_rate:.1%}",
            "max_daily_loss": self.config.max_daily_loss_usd,
            "max_positions": self.config.max_open_positions,
        }
