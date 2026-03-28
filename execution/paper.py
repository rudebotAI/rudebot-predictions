"""
Paper Trading Engine -- Simulates trades without real money.
Tracks entries, exits, P&L, and win rate.
"""

import json
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class PaperTrader:
    """Simulates trade execution and tracks paper P&L."""

    def __init__(self, config: dict):
        self.log_path = Path(config.get("trade_log", "logs/trades.json"))
        self.perf_path = Path(config.get("performance_log", "logs/performance.json"))
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.perf_path.parent.mkdir(parents=True, exist_ok=True)

        self.trades = self._load_trades()

    def _load_trades(self) -> dict:
        if self.log_path.exists():
            try:
                with open(self.log_path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"open": [], "closed": [], "skipped": []}

    def _save_trades(self):
        with open(self.log_path, "w") as f:
            json.dump(self.trades, f, indent=2)

    def _trade_id(self) -> str:
        raw = f"{datetime.now(timezone.utc).isoformat()}{len(self.trades['open'])}"
        return hashlib.md5(raw.encode()).hexdigest()[:8]

    def open_position(self, opportunity: dict, size_usd: float) -> dict:
        """
        Open a new paper position.

        Args:
            opportunity: Market opportunity dict from scanner
            size_usd: Dollar amount to invest

        Returns:
            Trade entry dict
        """
        price = opportunity.get("market_price", 0)
        shares = size_usd / price if price > 0 else 0

        trade = {
            "id": self._trade_id(),
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "platform": opportunity.get("platform", "unknown"),
            "question": opportunity.get("question", "unknown"),
            "signal": opportunity.get("signal", "YES"),
            "entry_price": price,
            "size_usd": size_usd,
            "shares": round(shares, 4),
            "ev_at_entry": opportunity.get("ev", 0),
            "model_prob": opportunity.get("model_prob", 0),
            "kelly_fraction": opportunity.get("kelly_fraction", 0),
            "condition_id": opportunity.get("condition_id", ""),
            "market_id": opportunity.get("market_id", ""),
            "token_id": (opportunity.get("token_ids", [None]) or [None])[0],
            "status": "open",
        }

        self.trades["open"].append(trade)
        self._save_trades()

        logger.info(
            f"[PAPER] Opened: {trade['signal']} {trade['question'][:50]} "
            f"@ {trade['entry_price']:.3f} | ${trade['size_usd']:.2f}"
        )
        return trade

    def close_position(self, trade_id: str, exit_price: float, reason: str = "manual") -> dict:
        """
        Close an open paper position.

        Args:
            trade_id: Trade ID to close
            exit_price: Current market price for exit
            reason: Why closing (manual, resolved, stop_loss, take_profit)

        Returns:
            Closed trade dict with P&L
        """
        trade = None
        for t in self.trades["open"]:
            if t["id"] == trade_id:
                trade = t
                break

        if not trade:
            logger.warning(f"Trade {trade_id} not found in open positions")
            return {}

        # Remove from open
        self.trades["open"] = [t for t in self.trades["open"] if t["id"] != trade_id]

        # Compute P&L
        entry = trade["entry_price"]
        if trade["signal"] == "YES":
            pnl_per_share = exit_price - entry
        else:
            pnl_per_share = entry - exit_price  # NO position profits when price drops

        pnl = pnl_per_share * trade["shares"]
        pnl_pct = (pnl / trade["size_usd"] * 100) if trade["size_usd"] > 0 else 0

        trade["closed_at"] = datetime.now(timezone.utc).isoformat()
        trade["exit_price"] = exit_price
        trade["pnl"] = round(pnl, 4)
        trade["pnl_pct"] = round(pnl_pct, 2)
        trade["close_reason"] = reason
        trade["status"] = "closed"

        self.trades["closed"].append(trade)
        self._save_trades()

        emoji = "+" if pnl >= 0 else ""
        logger.info(
            f"[PAPER] Closed: {trade['question'][:50]} "
            f"| {emoji}${pnl:.2f} ({emoji}{pnl_pct:.1f}%) | Reason: {reason}"
        )
        return trade

    def skip_opportunity(self, opportunity: dict, reason: str = "manual"):
        """Log a skipped opportunity for later review."""
        self.trades["skipped"].append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "question": opportunity.get("question", ""),
            "platform": opportunity.get("platform", ""),
            "ev": opportunity.get("ev", 0),
            "price": opportunity.get("market_price", 0),
            "reason": reason,
        })
        self._save_trades()

    def get_open_positions(self) -> list:
        return self.trades.get("open", [])

    def get_performance(self) -> dict:
        """Compute overall paper trading performance."""
        closed = self.trades.get("closed", [])
        if not closed:
            return {
                "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "total_pnl": 0, "avg_pnl": 0, "best_trade": 0, "worst_trade": 0,
                "open_positions": len(self.trades.get("open", [])),
            }

        pnls = [t.get("pnl", 0) for t in closed]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p <= 0)

        return {
            "total_trades": len(closed),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(closed) * 100, 1) if closed else 0,
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0,
            "best_trade": round(max(pnls), 2) if pnls else 0,
            "worst_trade": round(min(pnls), 2) if pnls else 0,
            "open_positions": len(self.trades.get("open", [])),
            "skipped": len(self.trades.get("skipped", [])),
        }

    def save_daily_performance(self):
        """Append today's performance to the daily log."""
        perf = self.get_performance()
        perf["date"] = datetime.now(timezone.utc).date().isoformat()

        history = []
        if self.perf_path.exists():
            try:
                with open(self.perf_path) as f:
                    history = json.load(f)
            except Exception:
                history = []

        history.append(perf)
        with open(self.perf_path, "w") as f:
            json.dump(history, f, indent=2)
