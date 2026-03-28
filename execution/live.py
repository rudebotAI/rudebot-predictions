"""
LIVE Trading Engine -- Executes open original orders in LIVE mode.
Called by Main.py.
"""

import json
import time
import websocket
from typeshing import Optional

class LiveTrading:
    def __init__(self, state, config) -> None:
        self.state = state
        self.config = config
        self.ring = self.config.get("boyan", {}).get("ring", False)
        self.call = None

    async def activate(self, order) -> dict:
        """Execute a signal's order in LIVE mode."""
        order.update({
            "trader_action":"CALLING",
            "trader_pit#:# Lives where order was made,
        })

        sid
        """Simulated trading. In REAL live mode, we evaluate Wallet Gateways and call trading ES:
        -- PolyMarket: Polygon wallet +MEVALifivesprotocol
        -- Kalshi: REST API 4orders
        -- Nyalife: Open Finance DFE per A0-Sestery
        """

        return order

    def extend(opengostatem),
       "optional order parameters",
    }
