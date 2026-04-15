"""
Main Loop — Python Async Prediction Bot v4.1
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
""" Main Loop — Python Async Prediction Bot v4.1
Uses env_config for secrets, risk_manager for position limits.
Integrates scanner.py for +EV detection and paper.py for trade execution.
"""
import os
import time
import sys
import asyncio
import logging
import json
from pathlib import Path
from datetime import datetime, timezone

from env_config import load_config
from risk_manager import RiskManager
from engines.scanner import EVScanner
from connectors.kalshi import KalshiConnector
from connectors.polymarket import PolymarketConnector
from execution.paper import PaperTrader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("predbot")


class PredMarketBot:
    def __init__(self):
        self.config = load_config()
        self.risk = RiskManager(self.config)
        self.scanner = EVScanner({"min_ev_threshold": self.config.risk.min_ev_threshold, "min_market_volume": 500})
        self.paper = PaperTrader(self.config.__dict__ if hasattr(self.config, '__dict__') else {})
        
        # Initialize connectors
        self.kalshi = KalshiConnector(self.config.kalshi.__dict__ if hasattr(self.config.kalshi, '__dict__') else {})
        self.polymarket = PolymarketConnector(self.config.polymarket.__dict__ if hasattr(self.config.polymarket, '__dict__') else {})
        
        # Paper trade log
        self.paper_trades_path = Path("logs/paper_trades.json")
        self.paper_trades_path.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Bot initialized in {self.config.mode} mode")
        logger.info(f"Platforms: {self.config.platforms}")
        logger.info(f"Risk limits: max_daily=${self.config.risk.max_daily_loss_usd}, max_pos=${self.config.risk.max_position_usd}")
        logger.info(f"Min EV threshold: {self.config.risk.min_ev_threshold}")

    async def scan_markets(self):
        """Scan configured platforms for +EV opportunities using EVScanner."""
        logger.info("Scanning markets...")
        
        opportunities = []
        
        # Fetch markets from enabled platforms
        markets = []
        if "kalshi" in self.config.platforms:
            try:
                k_markets = self.kalshi.scan_markets_with_prices(limit=50)
                markets.extend(k_markets)
                logger.info(f"Kalshi: fetched {len(k_markets)} markets")
            except Exception as e:
                logger.warning(f"Kalshi fetch failed: {e}")
        
        if "polymarket" in self.config.platforms:
            try:
                p_markets = self.polymarket.scan_markets_with_prices(limit=50)
                markets.extend(p_markets)
                logger.info(f"Polymarket: fetched {len(p_markets)} markets")
            except Exception as e:
                logger.warning(f"Polymarket fetch failed: {e}")
        
        if not markets:
            logger.warning("No markets fetched from any platform")
            status = self.risk.get_status()
            logger.info(f"Risk status: {json.dumps(status, indent=2)}")
            return []
        
        # Cross-reference markets across platforms
        if "kalshi" in self.config.platforms and "polymarket" in self.config.platforms:
            k_markets = [m for m in markets if m.get("platform") == "kalshi"]
            p_markets = [m for m in markets if m.get("platform") == "polymarket"]
            if k_markets and p_markets:
                markets = self.scanner.cross_reference_markets(p_markets, k_markets)
        
        # Scan for +EV opportunities
        opportunities = self.scanner.scan(markets)
        
        # Filter for conservative trading (prevent false positives)
        filtered_opps = []
        for opp in opportunities:
            edge = opp.get("edge", 0)
            ev = opp.get("ev", 0)
            if edge > 0.02 and ev > self.config.risk.min_ev_threshold:
                filtered_opps.append(opp)
                logger.info(f"Opportunity: {opp.get('question', 'unknown')[:50]} | EV={ev:.4f} | Edge={edge:.4f}")
        
        opportunities = filtered_opps
        
        if not opportunities:
            logger.info("No +EV opportunities found this scan cycle")
        
        # Log risk status
        status = self.risk.get_status()
        logger.info(f"Risk status: {json.dumps(status, indent=2)}")
        
        return opportunities

    def _get_position_size(self, opp: dict) -> float:
        """Determine position size using risk manager's constraints."""
        kelly_frac = self.config.risk.kelly_fraction
        edge = opp.get("edge", 0)
        
        if edge <= 0:
            return 5.0  # Minimum
        
        # Simple kelly-like sizing: 2% per EV point
        size = min(self.config.risk.max_position_usd, 10.0 + (edge * 100.0))
        return round(size, 2)

    async def run(self):
        """Main bot loop."""
        logger.info(f"Starting PredMarketBot v4.1 ({self.config.mode} mode)")
        logger.info(f"Scan interval: {self.config.scan_interval}s")
        
        while True:
            try:
                opportunities = await self.scan_markets()
                
                for opp in opportunities:
                    market_id = opp.get("market_id", "unknown")
                    size = self._get_position_size(opp)
                    side = opp.get("signal", "YES").upper()
                    price = opp.get("market_price", 0)
                    
                    allowed, reason = self.risk.can_trade(market_id, size, side.lower())
                    
                    if not allowed:
                        logger.info(f"Skipping {market_id}: {reason}")
                        self.paper.skip_opportunity(opp, reason)
                        continue
                    
                    if self.config.mode == "paper":
                        # Execute paper trade
                        trade = self.paper.open_position(opp, size)
                        logger.info(f"[PAPER] Would trade {market_id} {side} ${size:.2f} @ {price:.3f}")
                        self.risk.record_entry(market_id, price, int(size / price) if price > 0 else 0, side.lower(), size)
                        
                        # Log to paper trades file
                        self._log_paper_trade(trade)
                    else:
                        logger.warning(f"[LIVE] mode not enabled in this deployment")
                
                await asyncio.sleep(self.config.scan_interval)
                
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                break
            except Exception as e:
                logger.error(f"Scan cycle error: {e}", exc_info=True)
                await asyncio.sleep(self.config.scan_interval)

    def _log_paper_trade(self, trade: dict):
        """Log paper trades to JSON file."""
        try:
            trades = []
            if self.paper_trades_path.exists():
                with open(self.paper_trades_path) as f:
                    trades = json.load(f)
            
            trades.append({
                **trade,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            
            with open(self.paper_trades_path, "w") as f:
                json.dump(trades, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to log paper trade: {e}")


if __name__ == "__main__":
    bot = PredMarketBot()
    asyncio.run(bot.run())
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
