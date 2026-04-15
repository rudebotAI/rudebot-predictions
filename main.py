"""
Main Loop -- Python Async Prediction Bot v4.2
- v5 Kalshi connector (event-based scan, new field names)
- In-process HTTP dashboard at http://<host>/ (see dashboard.py)
- Close-loop: resolves open positions on market finalization + stop-loss/take-profit
"""
import os
import time
import sys
import asyncio
import logging
import json
from collections import deque
from pathlib import Path
from datetime import datetime, timezone

from env_config import load_config
from risk_manager import RiskManager
from engines.scanner import EVScanner
from connectors.kalshi import KalshiConnector
from connectors.polymarket import PolymarketConnector
from execution.paper import PaperTrader
import dashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("predbot")


def _to_float(x, default=0.0):
    if x is None:
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# Stop-loss: close YES at -50% entry, take-profit at +100% entry (for paper discipline)
STOP_LOSS_PCT = -0.50
TAKE_PROFIT_PCT = 1.00


class PredMarketBot:
    def __init__(self):
        self.config = load_config()
        self.risk = RiskManager(self.config)
        self.scanner = EVScanner({
            "min_ev_threshold": self.config.risk.min_ev_threshold,
            "min_market_volume": 100,
        })
        self.paper = PaperTrader(self.config.__dict__ if hasattr(self.config, '__dict__') else {})
        self.kalshi = KalshiConnector(self.config.kalshi.__dict__ if hasattr(self.config.kalshi, '__dict__') else {})
        self.polymarket = PolymarketConnector(self.config.polymarket.__dict__ if hasattr(self.config.polymarket, '__dict__') else {})

        self.paper_trades_path = Path("logs/paper_trades.json")
        self.paper_trades_path.parent.mkdir(parents=True, exist_ok=True)

        # Live dashboard state
        self._scan_number = 0
        self._last_scan_at = None
        self._last_kalshi_count = 0
        self._last_poly_count = 0
        self._last_ev_count = 0
        self._recent_signals = deque(maxlen=50)
        self._errors = deque(maxlen=20)

        logger.info(f"Bot initialized in {self.config.mode} mode")
        logger.info(f"Platforms: {self.config.platforms}")
        logger.info(f"Risk limits: max_daily=${self.config.risk.max_daily_loss_usd}, max_pos=${self.config.risk.max_position_usd}")
        logger.info(f"Min EV threshold: {self.config.risk.min_ev_threshold}")

    # ---------- Dashboard state provider ----------
    def _build_state(self) -> dict:
        perf = self.paper.get_performance()
        risk_status = "Active"
        try:
            rs = self.risk.get_status()
            if isinstance(rs, dict) and rs.get("halted"):
                risk_status = "Halted"
        except Exception:
            pass

        open_positions = self.paper.get_open_positions()
        closed = list(self.paper.trades.get("closed", []))
        closed.sort(key=lambda t: t.get("closed_at", ""), reverse=True)

        return {
            "mode": self.config.mode,
            "bankroll": getattr(self.config, "bankroll", 0),
            "scan_number": self._scan_number,
            "last_scan_at": self._last_scan_at,
            "kalshi_markets": self._last_kalshi_count,
            "poly_markets": self._last_poly_count,
            "ev_opportunities": self._last_ev_count,
            "arb_opportunities": 0,
            "risk_status": risk_status,
            "performance": perf,
            "open_positions": open_positions,
            "recent_closed": closed[:25],
            "recent_signals": list(self._recent_signals),
            "errors": list(self._errors),
        }

    # ---------- Scanning ----------
    async def scan_markets(self):
        logger.info("Scanning markets...")
        markets = []
        k_count = 0
        p_count = 0

        if "kalshi" in self.config.platforms:
            try:
                k_markets = self.kalshi.scan_markets_with_prices(limit=50)
                markets.extend(k_markets)
                k_count = len(k_markets)
                logger.info(f"Kalshi: fetched {k_count} markets")
            except Exception as e:
                msg = f"Kalshi fetch failed: {e}"
                logger.warning(msg)
                self._errors.append(msg)

        if "polymarket" in self.config.platforms:
            try:
                p_markets = self.polymarket.scan_markets_with_prices(limit=50)
                markets.extend(p_markets)
                p_count = len(p_markets)
                logger.info(f"Polymarket: fetched {p_count} markets")
            except Exception as e:
                msg = f"Polymarket fetch failed: {e}"
                logger.warning(msg)
                self._errors.append(msg)

        self._last_kalshi_count = k_count
        self._last_poly_count = p_count

        if not markets:
            logger.warning("No markets fetched from any platform")
            return []

        if "kalshi" in self.config.platforms and "polymarket" in self.config.platforms:
            k = [m for m in markets if m.get("platform") == "kalshi"]
            p = [m for m in markets if m.get("platform") == "polymarket"]
            if k and p:
                markets = self.scanner.cross_reference_markets(p, k)

        opportunities = self.scanner.scan(markets)

        filtered = []
        for opp in opportunities:
            edge = opp.get("edge", 0)
            ev = opp.get("ev", 0)
            if edge > 0.005 and ev > self.config.risk.min_ev_threshold:
                filtered.append(opp)
                logger.info(f"Opportunity: {opp.get('question','unknown')[:50]} | EV={ev:.4f} | Edge={edge:.4f}")

        self._last_ev_count = len(filtered)
        for opp in filtered:
            self._recent_signals.append({
                "question": opp.get("question", ""),
                "signal": opp.get("signal", "YES"),
                "ev": opp.get("ev", 0),
                "edge": opp.get("edge", 0),
                "size_usd": self._get_position_size(opp),
                "platform": opp.get("platform", ""),
            })

        if not filtered:
            logger.info("No +EV opportunities found this scan cycle")
        return filtered

    def _get_position_size(self, opp: dict) -> float:
        edge = opp.get("edge", 0)
        if edge <= 0:
            return 5.0
        size = min(self.config.risk.max_position_usd, 10.0 + (edge * 100.0))
        return round(size, 2)

    # ---------- Close-loop ----------
    def _check_closures(self):
        """Close paper positions on market resolution, stop-loss, or take-profit."""
        open_positions = list(self.paper.get_open_positions())
        if not open_positions:
            return

        closed_now = 0
        for pos in open_positions:
            platform = pos.get("platform", "")
            market_id = pos.get("market_id", "")
            if not market_id:
                continue

            m = None
            if platform == "kalshi":
                try:
                    m = self.kalshi.get_market(market_id)
                except Exception as e:
                    logger.debug(f"closures: kalshi get_market({market_id}) failed: {e}")
                    continue
            else:
                # polymarket close-loop can be added later; skip for now
                continue

            if not m:
                continue

            status = (m.get("status") or "").lower()
            result = (m.get("result") or "").lower()

            # Resolution
            if status in ("finalized", "settled", "closed") and result in ("yes", "no"):
                signal = (pos.get("signal") or "YES").upper()
                win = (signal == "YES" and result == "yes") or (signal == "NO" and result == "no")
                exit_price = 1.0 if win else 0.0
                self.paper.close_position(pos["id"], exit_price, reason="resolved")
                closed_now += 1
                continue

            # Stop-loss / take-profit using current mid
            yb = _to_float(m.get("yes_bid_dollars"))
            ya = _to_float(m.get("yes_ask_dollars"))
            lp = _to_float(m.get("last_price_dollars"))
            if yb > 0 and ya > 0:
                mid = (yb + ya) / 2
            else:
                mid = lp or yb or ya
            if not mid:
                continue

            entry = float(pos.get("entry_price") or 0)
            if entry <= 0:
                continue
            signal = (pos.get("signal") or "YES").upper()
            if signal == "YES":
                pnl_pct = (mid - entry) / entry
            else:
                pnl_pct = (entry - mid) / entry

            if pnl_pct <= STOP_LOSS_PCT:
                self.paper.close_position(pos["id"], mid, reason="stop_loss")
                closed_now += 1
            elif pnl_pct >= TAKE_PROFIT_PCT:
                self.paper.close_position(pos["id"], mid, reason="take_profit")
                closed_now += 1

        if closed_now:
            logger.info(f"closures: closed {closed_now} position(s)")

    # ---------- Main loop ----------
    async def run(self):
        # Start dashboard HTTP server once
        dashboard.set_state_provider(self._build_state)
        try:
            dashboard.start()
        except Exception as e:
            logger.warning(f"dashboard failed to start: {e}")

        logger.info(f"Starting PredMarketBot v4.2 ({self.config.mode} mode)")
        logger.info(f"Scan interval: {self.config.scan_interval}s")

        while True:
            try:
                self._scan_number += 1
                self._last_scan_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

                # 1. Close any positions that need closing
                try:
                    self._check_closures()
                except Exception as e:
                    msg = f"closures error: {e}"
                    logger.error(msg, exc_info=True)
                    self._errors.append(msg)

                # 2. Scan and take new positions
                opportunities = await self.scan_markets()
                for opp in opportunities:
                    market_id = opp.get("market_id", "unknown")
                    size = self._get_position_size(opp)
                    side = opp.get("signal", "YES").upper()
                    price = opp.get("market_price", 0) or opp.get("yes_price", 0)

                    allowed, reason = self.risk.can_trade(market_id, size, side.lower())
                    if not allowed:
                        logger.info(f"Skipping {market_id}: {reason}")
                        self.paper.skip_opportunity(opp, reason)
                        continue

                    if self.config.mode == "paper":
                        trade = self.paper.open_position(opp, size)
                        logger.info(f"[PAPER] Would trade {market_id} {side} ${size:.2f} @ {price:.3f}")
                        self.risk.record_entry(market_id, price, int(size / price) if price > 0 else 0, side.lower(), size)
                        self._log_paper_trade(trade)
                    else:
                        logger.warning(f"[LIVE] mode not enabled in this deployment")

                await asyncio.sleep(self.config.scan_interval)
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                break
            except Exception as e:
                msg = f"Scan cycle error: {e}"
                logger.error(msg, exc_info=True)
                self._errors.append(msg)
                await asyncio.sleep(self.config.scan_interval)

    def _log_paper_trade(self, trade: dict):
        try:
            trades = []
            if self.paper_trades_path.exists():
                with open(self.paper_trades_path) as f:
                    trades = json.load(f)
            trades.append({**trade, "timestamp": datetime.now(timezone.utc).isoformat()})
            with open(self.paper_trades_path, "w") as f:
                json.dump(trades, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to log paper trade: {e}")


if __name__ == "__main__":
    bot = PredMarketBot()
    asyncio.run(bot.run())
