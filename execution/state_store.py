"""
State Store -- Persistent state management for the bot.
Matches the diagram: central state layer tracking positions, running P&L,
price cache, and market snapshots.

Writes to disk after every update so the bot can resume after restart.
"""

import json
import time
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class StateStore:
    """
    Persistent state store. Tracks:
    - Open positions (paper + live)
    - Running P&L (daily, total)
    - Price cache (latest prices per market)
    - Market snapshots (last scan results)
    - Wallet balances
    """

    def __init__(self, state_file: str = "logs/state.json"):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load()

    def _load(self) -> dict:
        """Load state from disk or create fresh."""
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    data = json.load(f)
                    logger.info(f"State loaded: {len(data.get('positions', []))} positions")
                    return data
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"State file corrupt, starting fresh: {e}")

        return {
            "positions": [],
            "closed_trades": [],
            "daily_pnl": 0.0,
            "total_pnl": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "price_cache": {},
            "market_snapshots": [],
            "wallet_balance": 0.0,
            "last_scan_time": None,
            "scan_count": 0,
            "errors": [],
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": None,
        }

    def _save(self):
        """Persist state to disk."""
        self.state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.state_file, "w") as f:
                json.dump(self.state, f, indent=2, default=str)
        except IOError as e:
            logger.error(f"Failed to save state: {e}")

    # ── Positions ──

    def add_position(self, position: dict):
        """Track a new open position."""
        self.state["positions"].append(position)
        self._save()

    def remove_position(self, position_id: str) -> Optional[dict]:
        """Remove a position by ID and move to closed_trades."""
        for i, p in enumerate(self.state["positions"]):
            if p.get("id") == position_id:
                closed = self.state["positions"].pop(i)
                self.state["closed_trades"].append(closed)
                self._save()
                return closed
        return None

    def get_positions(self) -> list:
        return self.state.get("positions", [])

    def get_closed_trades(self, limit: int = 50) -> list:
        return self.state.get("closed_trades", [])[-limit:]

    # ── P&L ──

    def record_pnl(self, pnl: float, is_win: bool):
        """Record a trade result."""
        self.state["total_pnl"] += pnl
        self.state["daily_pnl"] += pnl
        self.state["total_trades"] += 1
        if is_win:
            self.state["wins"] += 1
        else:
            self.state["losses"] += 1
        self._save()

    def reset_daily_pnl(self):
        """Reset daily P&L (call at midnight or bot restart)."""
        self.state["daily_pnl"] = 0.0
        self._save()

    def get_pnl_summary(self) -> dict:
        total = self.state["total_trades"]
        return {
            "total_pnl": self.state["total_pnl"],
            "daily_pnl": self.state["daily_pnl"],
            "total_trades": total,
            "wins": self.state["wins"],
            "losses": self.state["losses"],
            "win_rate": (self.state["wins"] / total * 100) if total > 0 else 0,
        }

    # ── Price Cache ──

    def update_price(self, market_id: str, price: float, source: str = ""):
        """Cache a market price."""
        self.state["price_cache"][market_id] = {
            "price": price,
            "source": source,
            "time": time.time(),
        }
        # Don't save on every price update -- too noisy
        # Caller can batch save via save()

    def get_cached_price(self, market_id: str, max_age: int = 120) -> Optional[float]:
        """Get cached price if fresh enough."""
        entry = self.state["price_cache"].get(market_id)
        if entry and (time.time() - entry["time"]) < max_age:
            return entry["price"]
        return None

    # ── Market Snapshots ──

    def save_snapshot(self, markets: list, opportunities: list, arbs: list):
        """Save a scan cycle snapshot (keep last 10)."""
        self.state["market_snapshots"].append({
            "time": time.strftime("%H:%M:%S"),
            "total_markets": len(markets),
            "opportunities": len(opportunities),
            "arbs": len(arbs),
            "top_opp": opportunities[0] if opportunities else None,
        })
        self.state["market_snapshots"] = self.state["market_snapshots"][-10:]
        self.state["scan_count"] += 1
        self.state["last_scan_time"] = time.strftime("%H:%M:%S")
        self._save()

    # ── Wallet ──

    def update_wallet_balance(self, balance: float):
        self.state["wallet_balance"] = balance
        self._save()

    def get_wallet_balance(self) -> float:
        return self.state.get("wallet_balance", 0)

    # ── Errors ──

    def log_error(self, error: str):
        self.state["errors"].append({
            "msg": error[:200],
            "time": time.strftime("%H:%M:%S"),
        })
        self.state["errors"] = self.state["errors"][-20:]
        self._save()

    # ── Full State ──

    def get_full_state(self) -> dict:
        """Return full state for dashboard."""
        return {
            **self.get_pnl_summary(),
            "open_positions": len(self.state["positions"]),
            "wallet_balance": self.state["wallet_balance"],
            "scan_count": self.state["scan_count"],
            "last_scan": self.state["last_scan_time"],
            "recent_errors": self.state["errors"][-5:],
        }

    def save(self):
        """Explicit save (for batch operations)."""
        self._save()
