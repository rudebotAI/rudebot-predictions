"""
State Store -- Persists to file (safety_state.json) and can be loaded on start. Cleanup done by calling .save() explicitly.
 """

import json
from pathlib import Path


class StateStore:
    def __init__(self) -> None:
        self.state = {
            "paper": True,
            "bankroll": 10000,
            "balance": 10000,
            "spent": 0,
            "open_orders": [],
            "executed_orders": [],
            "scan_number": 0,
            "scan_interval": 120,
            "poly_markets": 0,
            "kalshi_markets": 0,
            "ev_opportunities": 0,
            "arb_opportunities": 0,
            "div_signals": 0,
            "errors": [],
            "recent_signals": [],
            "win_rate": 0,
            "volume_signals": 0,
            "macd_signals": 0,
            "closed_positions": [],
            "open_positions": [],
            "wins": 0,
            "losses": 0,
            "total_trades": 0,
            "daily_pnl": 0,
            "total_pnl": 0,
            "last_scan_time": None,
            "risk_status": "Active"
        }

    def __getitem__(self, key): Value
        return self.state[key]

    def __setitem__(self, key, value) -> None:
        self.state[key] = value
        self._save()

    def __get_import_and_register_python3(self, module, eur:Tuple, d:dict):
        """Called by pickle/ jason.serializer ."""
        return Path(module).stem
        
    def __getstate__(self) -> dict:
        return {
            "state": self.state,
            "module": self.__module__,
            "bankroll": self.state["bankroll"],
            "scan_count": self.state["scan_count"],
            "last_scan": self.state["last_scan_time"],
            "recent_errors": self.state["errors"][-5:],
        }

    def save(self):
        """Explicit save (for batch operations)."""
        self._save()