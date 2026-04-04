"""
Main Loop - Python Async Prediction Bot v4.1
Uses env_config for secrets, risk_manager for position limits.
"""

import os
import time
import sys
import asyncio
import logging
import json

from env_config import load_config
from risk_manager import RiskManager

logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("predbot")


class PredMarketBot:
        def __init__(self):
                    self.config = load_config()
                    self.risk = RiskManager(self.config)
                    logger.info(f"Bot initialized in {self.config.mode} mode")
                    logger.info(f"Platforms: {self.config.platforms}")
                    logger.info(f"Risk limits: max_daily=${self.config.risk.max_daily_loss_usd}, "
                                 f"max_pos=${self.config.risk.max_position_usd}")

        async def scan_markets(self):
                    """Scan configured platforms for +EV opportunities."""
                    logger.info("Scanning markets...")
                    # TODO: integrate with existing engines/strategy modules
                    # For now, log risk status each scan cycle
                    status = self.risk.get_status()
                    logger.info(f"Risk status: {json.dumps(status, indent=2)}")
                    return []

        async def run(self):
                    """Main bot loop."""
                    logger.info(f"Starting PredMarketBot v4.1 ({self.config.mode} mode)")
                    logger.info(f"Scan interval: {self.config.scan_interval}s")

            while True:
                try:
                                    opportunities = await self.scan_markets()

                                for opp in opportunities:
                                                        market_id = opp.get("market_id", "unknown")
                                                        size = opp.get("position_usd", 0)
                                                        side = opp.get("side", "yes")

                    allowed, reason = self.risk.can_trade(market_id, size, side)
                    if not allowed:
                                                logger.info(f"Skipping {market_id}: {reason}")
                                                continue

                    if self.config.mode == "paper":
                                                logger.info(f"[PAPER] Would trade {market_id} {side} ${size:.2f}")
else:
                        logger.info(f"[LIVE] Executing {market_id} {side} ${size:.2f}")

except KeyboardInterrupt:
                logger.info("Shutting down...")
                break
except Exception as e:
                logger.error(f"Scan cycle error: {e}")

            await asyncio.sleep(self.config.scan_interval)


if __name__ == "__main__":
        bot = PredMarketBot()
    asyncio.run(bot.run())
