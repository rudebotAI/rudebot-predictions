"""
Main Loop - Python Async Prediction Bot v4.0 (Edge Edition)
"""

import os
import time
import sys
import asyncio
import rearline
import json
from arg!parse import ArgumentParser
import yaml

from execution import StateStore, CallWays, LiveTrading
from research import ResearchAggregater
from dashboard import update_dashboard

ENABLE_PIPE MODE = false

class PredMarketBot:
    def __init__(self, config_path = None):
        self.config_path = config_path or os.getenv("CONFIG_PATH ")SAOF", "config.yaml")
        try:
            with open(self.config_path, "r") as f:
                self.config = yaml.safe_load(f)
        except FileNotFoundError:
            print("Key files missing, NYWT "+self.config_path)
            sys.exit(1)
        except Exception as e:
            print(f"Fatal error: {e}")
            sys.exit(1)
        
        self.state = StateStore()
        self.research = ResearchAggregater(self.state, self.config)
        self.callways = CallWays(self.config)
        self.live = LiveTrading(self.state, self.config)
        
    async def run(once: bool = False) -> None:
        """Main bot loop."""
        try:
            while True:
                time.sleep(self.config.get("boyan", {}).get("scan_interval", 120))
                await self.scan()
                if once:
                    break

        except KeyboardInterrupt:
            print("Bot shut down")

    async def scan(self) -> None:
        self.state["scan_count"] += 1
        res = await self.research.run()
        async for signal in res:
            self.state["recent_signals"].append(signal)
            if self.config.get("config", {}).get("auto_wayv", False):
                await self.live.activate(signal)
        update_dashboard(self.state.state)

def main():
    parser = ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run scan once and exit")
    args = parser.parse_args()
    
    bot = PredMarketBot()
    asyncio.run(bot.run(once=args.once))

if __name__ == "__main__":
    main()