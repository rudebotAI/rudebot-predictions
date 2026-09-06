"""
Position Book / Paper Trading Engine.

Used for paper trading AND as the position ledger for live trading (one
instance per mode, different log files). Tracks entries, exits, P&L,
win rate, per-trade returns and Sharpe.

Price convention (v6): every price stored or passed in is in the
POSITION'S OWN LEG. A NO position entered at 0.30 has entry_price 0.30,
and must be closed with the NO price (1 - yes_mid, or 1.0/0.0 on
resolution). P&L is therefore always `exit - entry` per share. The old
"NO profits when price drops" formula mixed legs and inverted every NO
trade's P&L.
"""

import json
import hashlib
import logging
import math
import threading
from datetime import datetime, timezone
from pathlib import Path

from risk_manager import atomic_write_json

logger = logging.getLogger(__name__)


class PaperTrader:
    """Position ledger. Simulates execution in paper mode; records fills in live mode."""

    def __init__(self, config: dict):
        self.log_path = Path(config.get("trade_log", "logs/trades.json"))
        self.perf_path = Path(config.get("performance_log", "logs/performance.json"))
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.perf_path.parent.mkdir(parents=True, exist_ok=True)
        self.label = config.get("label", "PAPER")
        self._lock = threading.RLock()

        self.trades = self._load_trades()

    def _load_trades(self) -> dict:
        if self.log_path.exists():
            try:
                with open(self.log_path) as f:
                    data = json.load(f)
                for k in ("open", "closed", "skipped"):
                    data.setdefault(k, [])
                return data
            except Exception as e:
                logger.error(f"Could not load {self.log_path}: {e} -- starting empty")
        return {"open": [], "closed": [], "skipped": []}

    def _save_trades(self):
        try:
            atomic_write_json(self.log_path, self.trades)
        except Exception as e:
            logger.error(f"Failed to save trades: {e}")

    def _trade_id(self) -> str:
        raw = f"{datetime.now(timezone.utc).isoformat()}{len(self.trades['open'])}"
        return hashlib.md5(raw.encode()).hexdigest()[:8]

    def open_position(self, opportunity: dict, size_usd: float,
                      fill: dict = None) -> dict:
        """
        Open a position.

        Args:
            opportunity: Market opportunity dict from scanner
            size_usd: Dollar amount to invest (paper) / intended (live)
            fill: optional live fill {price, contracts, size_usd, order_id}
                  overriding the paper assumptions.

        Returns:
            Trade entry dict
        """
        price = float(opportunity.get("market_price", 0) or 0)
        shares = size_usd / price if price > 0 else 0
        order_id = None
        fee = 0.0
        if fill:
            price = float(fill.get("price", price) or price)
            shares = float(fill.get("contracts", shares) or shares)
            size_usd = float(fill.get("size_usd", shares * price) or shares * price)
            order_id = fill.get("order_id")
            fee = float(fill.get("fee_per_contract", 0) or 0) * shares

        trade = {
            "id": self._trade_id(),
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "platform": opportunity.get("platform", "unknown"),
            "question": opportunity.get("question", "unknown"),
            "signal": str(opportunity.get("signal", "YES")).upper(),
            "entry_price": round(price, 4),
            "size_usd": round(size_usd, 4),
            "shares": round(shares, 4),
            "fees_usd": round(fee, 4),
            "ev_at_entry": opportunity.get("ev", 0),
            "edge_at_entry": opportunity.get("edge", 0),
            "model_prob": opportunity.get("model_prob", 0),
            "kelly_raw": opportunity.get("kelly_raw", 0),
            "kelly_fractional": opportunity.get("kelly_fractional", 0),
            "market_id": opportunity.get("market_id", ""),
            "event_ticker": opportunity.get("event_ticker", ""),
            "order_id": order_id,
            "status": "open",
        }

        with self._lock:
            self.trades["open"].append(trade)
            self._save_trades()

        logger.info(
            f"[{self.label}] Opened: {trade['signal']} {trade['question'][:50]} "
            f"@ {trade['entry_price']:.4f} | ${trade['size_usd']:.2f}"
        )
        return trade

    def close_position(self, trade_id: str, exit_price: float, reason: str = "manual",
                       shares: float = None, fees_usd: float = 0.0) -> dict:
        """
        Close an open position (fully, or partially when `shares` is given).

        Args:
            trade_id: Trade ID to close
            exit_price: Exit price IN THE POSITION'S OWN LEG (NO price for a NO)
            reason: Why closing (manual, resolved, stop_loss, take_profit)
            shares: contracts closed (default: all)
            fees_usd: exit fees to subtract from P&L

        Returns:
            Closed trade dict with P&L (empty dict if not found)
        """
        with self._lock:
            trade = next((t for t in self.trades["open"] if t["id"] == trade_id), None)
            if not trade:
                logger.warning(f"Trade {trade_id} not found in open positions")
                return {}

            total_shares = float(trade["shares"])
            qty = total_shares if shares is None else min(float(shares), total_shares)
            if qty <= 0:
                return {}
            entry = float(trade["entry_price"])
            entry_fees = float(trade.get("fees_usd", 0) or 0) * (qty / total_shares)
            # Same-leg prices => P&L is always exit - entry, net of both legs' fees.
            pnl = (exit_price - entry) * qty - float(fees_usd or 0) - entry_fees
            stake = entry * qty
            pnl_pct = (pnl / stake * 100) if stake > 0 else 0

            partial = qty < total_shares - 1e-9
            if partial:
                trade["shares"] = round(total_shares - qty, 4)
                trade["size_usd"] = round(trade["shares"] * entry, 4)
                trade["fees_usd"] = round(float(trade.get("fees_usd", 0) or 0) - entry_fees, 4)
                closed = dict(trade)
                closed["id"] = f"{trade['id']}-p{len(self.trades['closed'])}"
                closed["shares"] = round(qty, 4)
                closed["size_usd"] = round(stake, 4)
                closed["fees_usd"] = round(entry_fees, 4)
            else:
                self.trades["open"] = [t for t in self.trades["open"] if t["id"] != trade_id]
                closed = trade

            closed["closed_at"] = datetime.now(timezone.utc).isoformat()
            closed["exit_price"] = round(exit_price, 4)
            closed["pnl"] = round(pnl, 4)
            closed["pnl_pct"] = round(pnl_pct, 2)
            closed["exit_fees_usd"] = round(float(fees_usd or 0), 4)
            closed["close_reason"] = reason
            closed["status"] = "closed"
            closed["partial"] = partial

            self.trades["closed"].append(closed)
            self._save_trades()

        sign = "+" if pnl >= 0 else ""
        logger.info(
            f"[{self.label}] Closed{' (partial)' if partial else ''}: {closed['question'][:50]} "
            f"| {sign}${pnl:.2f} ({sign}{pnl_pct:.1f}%) | Reason: {reason}"
        )
        return closed

    def skip_opportunity(self, opportunity: dict, reason: str = "manual"):
        """Log a skipped opportunity for later review."""
        with self._lock:
            self.trades["skipped"].append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "question": opportunity.get("question", ""),
                "market_id": opportunity.get("market_id", ""),
                "platform": opportunity.get("platform", ""),
                "ev": opportunity.get("ev", 0),
                "edge": opportunity.get("edge", 0),
                "price": opportunity.get("market_price", 0),
                "reason": reason,
            })
            # Keep the skipped log bounded.
            if len(self.trades["skipped"]) > 500:
                self.trades["skipped"] = self.trades["skipped"][-500:]
            self._save_trades()

    def get_open_positions(self) -> list:
        with self._lock:
            return list(self.trades.get("open", []))

    def get_closed_positions(self) -> list:
        with self._lock:
            return list(self.trades.get("closed", []))

    def get_performance(self) -> dict:
        """Compute overall performance (incl. per-trade Sharpe)."""
        with self._lock:
            closed = list(self.trades.get("closed", []))
            n_open = len(self.trades.get("open", []))
            n_skipped = len(self.trades.get("skipped", []))

        if not closed:
            return {
                "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "total_pnl": 0, "avg_pnl": 0, "best_trade": 0, "worst_trade": 0,
                "sharpe": None, "open_positions": n_open, "skipped": n_skipped,
            }

        pnls = [t.get("pnl", 0) for t in closed]
        rets = [t.get("pnl_pct", 0) / 100.0 for t in closed]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p <= 0)
        sharpe = None
        if len(rets) >= 2:
            mean = sum(rets) / len(rets)
            sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
            sharpe = round(mean / sd, 3) if sd > 0 else None

        return {
            "total_trades": len(closed),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(closed) * 100, 1),
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / len(pnls), 2),
            "best_trade": round(max(pnls), 2),
            "worst_trade": round(min(pnls), 2),
            "sharpe": sharpe,
            "open_positions": n_open,
            "skipped": n_skipped,
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
        try:
            atomic_write_json(self.perf_path, history)
        except Exception as e:
            logger.error(f"Failed to save performance log: {e}")
