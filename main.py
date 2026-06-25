"""
Main Loop -- Python Async Prediction Bot v4.3
- Kalshi-only (Polymarket removed)
- In-process HTTP dashboard at http://<host>/ (see dashboard.py)
- Close-loop: resolves open positions on market finalization + stop-loss/take-profit
- v4.3: relaxed EV_SANITY_CAP from 2.0 -> 5.0 (matches scanner.compute_ev clamp)
"""
import asyncio
import json
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import dashboard
from connectors.kalshi import KalshiConnector
from engines.scanner import EVScanner
from engines.cross_reference import enrich_with_cross_reference
from env_config import load_config
from execution.paper import PaperTrader
from execution.live import LiveTrader
from connectors.polymarket_trader import PolymarketTrader
from execution.multi_venue import PolymarketLiveTrader, MultiVenueExecutor
from alerts.telegram import TelegramAlerts
from risk_manager import RiskManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("predbot")


def _to_float(x, default=0.0):
    if x is None:
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# Stop-loss: close YES at -50% entry, take-profit at +100% entry (paper discipline)
STOP_LOSS_PCT = -0.50
TAKE_PROFIT_PCT = 1.00

# EV sanity cap. The scanner already clamps single-trade EV to 5.0; this gate
# kicks in only when something truly broken happens upstream (e.g. all 50
# markets in a scan come back with EV at the cap, suggesting a unit/formula
# bug rather than a real opportunity).
EV_SANITY_CAP = 5.0


def _setup_persistence():
    """Symlink logs/ -> /data/logs when a Railway Volume is mounted at /data."""
    data_dir = Path("/data")
    logs_dir = Path("logs")
    if not (data_dir.exists() and data_dir.is_dir()):
        return
    persistent = data_dir / "logs"
    persistent.mkdir(parents=True, exist_ok=True)

    if logs_dir.is_symlink():
        return

    if logs_dir.exists():
        for f in logs_dir.iterdir():
            target = persistent / f.name
            if not target.exists():
                try:
                    f.rename(target)
                except OSError:
                    pass
        try:
            logs_dir.rmdir()
        except OSError:
            pass

    if not logs_dir.exists():
        try:
            logs_dir.symlink_to(persistent, target_is_directory=True)
            logger.info(f"persistence: logs/ -> {persistent} (Railway Volume)")
        except OSError as e:
            logger.warning(f"persistence: symlink failed, falling back to ephemeral: {e}")


class PredMarketBot:
    def __init__(self):
        _setup_persistence()
        self.config = load_config()
        self.risk = RiskManager(self.config)
        self.scanner = EVScanner({
            "min_ev_threshold": self.config.risk.min_ev_threshold,
            "min_market_volume": 100,
            "max_days_to_resolution": self.config.risk.max_days_to_resolution,
        })
        self.paper = PaperTrader(
            self.config.__dict__ if hasattr(self.config, "__dict__") else {}
        )
        self.kalshi = KalshiConnector(
            self.config.kalshi.__dict__ if hasattr(self.config.kalshi, "__dict__") else {}
        )

        # Telegram confirm/alert channel + live executor.
        self.telegram = TelegramAlerts({
            "bot_token": self.config.telegram.bot_token,
            "chat_id": self.config.telegram.chat_id,
            "require_confirm": getattr(self.config.telegram, "require_confirm", True),
        })
        self.polymarket_trader = PolymarketTrader()
        _kalshi_live = LiveTrader(self.kalshi, self.risk)
        _poly_live = PolymarketLiveTrader(self.polymarket_trader, self.risk)
        self.live = MultiVenueExecutor(_kalshi_live, _poly_live)
        self._live_ready = False
        if self.config.mode == "live":
            self._live_ready = self._preflight_live()

        self.paper_trades_path = Path("logs/paper_trades.json")
        self.paper_trades_path.parent.mkdir(parents=True, exist_ok=True)

        self._scan_number = 0
        self._last_scan_at = None
        self._last_kalshi_count = 0
        self._last_poly_count = 0
        self._last_ev_count = 0
        self._recent_signals = deque(maxlen=50)
        self._errors = deque(maxlen=20)
        # Market IDs with an outstanding live confirm request (avoid re-nagging).
        self._pending_live = set()

        logger.info(f"Bot initialized in {self.config.mode} mode")
        logger.info(f"Platforms: {self.config.platforms}")
        logger.info(
            f"Risk limits: max_daily=${self.config.risk.max_daily_loss_usd}, "
            f"max_pos=${self.config.risk.max_position_usd}"
        )
        logger.info(f"Min EV threshold: {self.config.risk.min_ev_threshold}")

    def _preflight_live(self) -> bool:
        """Hard gate for live trading. ALL must hold or we refuse to trade.

        Fails safe: if any precondition is missing we log CRITICAL and return
        False. The run loop then scans only and never places an order. We never
        silently fall back to paper writes or auto-execute without confirm.
        """
        problems = []
        if not self.config.telegram.bot_token or not self.config.telegram.chat_id:
            problems.append("Telegram not configured (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)")
        if not getattr(self.config.telegram, "require_confirm", True):
            problems.append("require_confirm is False (per-trade confirm is mandatory for live)")
        if not self.kalshi.has_trading_auth():
            problems.append("Kalshi trading auth missing (KALSHI_ACCESS_KEY + KALSHI_PRIVATE_KEY)")

        if problems:
            for p in problems:
                logger.critical(f"[LIVE] BLOCKED — {p}")
            logger.critical(
                "[LIVE] Live mode requested but preconditions not met. "
                "Bot will SCAN ONLY and place no orders."
            )
            return False

        self.live.enable()
        logger.critical(
            "[LIVE] Preconditions met. Live trading armed. Every trade still "
            "requires explicit Telegram confirmation."
        )
        return True

    def _process_live_confirmations(self):
        """Execute trades the user confirmed via Telegram since the last poll."""
        if not (self.config.mode == "live" and self._live_ready):
            return
        try:
            confirmed = self.telegram.poll_callbacks()
        except Exception as e:
            logger.error(f"[LIVE] Telegram poll failed: {e}")
            return
        for info in confirmed:
            opp = info.get("opp", {})
            size_usd = float(info.get("sizing", {}).get("size_usd", 0) or 0)
            market_id = opp.get("market_id", "unknown")
            # Confirmation consumed; allow this market to be re-offered later.
            self._pending_live.discard(market_id)
            result = self.live.execute(opp, size_usd)
            if result.get("success"):
                logger.info(f"[LIVE] Executed {market_id}: {result}")
                try:
                    self.telegram.send_trade_opened({
                        "market_id": market_id,
                        "side": result.get("side"),
                        "price": result.get("price"),
                        "size_usd": result.get("size_usd"),
                        "contracts": result.get("contracts"),
                    })
                except Exception:
                    pass
            else:
                logger.warning(f"[LIVE] Order not placed for {market_id}: {result.get('error')}")

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

        self._last_kalshi_count = k_count
        p_count = enrich_with_cross_reference(markets, self.config, logger)
        self._last_poly_count = p_count
        if os.getenv("POLYMARKET_TRADING", "").strip().lower() in ("1", "true", "yes", "on"):
            try:
                pm_tradable = self.polymarket_trader.fetch_tradable_markets(limit=100)
                markets.extend(pm_tradable)
                logger.info(f"Polymarket: added {len(pm_tradable)} tradable markets to scan")
            except Exception as ex:
                logger.warning(f"Polymarket tradable fetch failed: {ex}")

        if not markets:
            logger.warning("No markets fetched from any platform")
            return []

        opportunities = self.scanner.scan(markets)

        filtered = []
        bogus = 0
        for opp in opportunities:
            edge = opp.get("edge", 0)
            ev = opp.get("ev", 0)
            if ev >= EV_SANITY_CAP:
                bogus += 1
                continue
            if edge > 0.005 and ev > self.config.risk.min_ev_threshold:
                filtered.append(opp)
                logger.info(
                    f"Opportunity: {opp.get('question','unknown')[:50]} | "
                    f"EV={ev:.4f} | Edge={edge:.4f}"
                )
        if bogus:
            msg = (
                f"scanner returned {bogus} opportunity(ies) at EV cap ({EV_SANITY_CAP}) -- "
                "may indicate upstream price feed lag; skipped"
            )
            logger.warning(msg)
            self._errors.append(msg)

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

    def _check_closures(self):
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
            if not m:
                continue

            status = (m.get("status") or "").lower()
            result = (m.get("result") or "").lower()

            if status in ("finalized", "settled", "closed") and result in ("yes", "no"):
                signal = (pos.get("signal") or "YES").upper()
                win = (signal == "YES" and result == "yes") or (signal == "NO" and result == "no")
                exit_price = 1.0 if win else 0.0
                self.paper.close_position(pos["id"], exit_price, reason="resolved")
                closed_now += 1
                continue

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

    async def run(self):
        dashboard.set_state_provider(self._build_state)
        try:
            dashboard.start()
        except Exception as e:
            logger.warning(f"dashboard failed to start: {e}")

        logger.info(f"Starting PredMarketBot v4.3 ({self.config.mode} mode)")
        logger.info(f"Scan interval: {self.config.scan_interval}s")

        while True:
            try:
                self._scan_number += 1
                self._last_scan_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

                try:
                    self._check_closures()
                except Exception as e:
                    msg = f"closures error: {e}"
                    logger.error(msg, exc_info=True)
                    self._errors.append(msg)

                # Execute any trades the user confirmed via Telegram since last cycle.
                self._process_live_confirmations()

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
                        logger.info(
                            f"[PAPER] Would trade {market_id} {side} ${size:.2f} @ {price:.3f}"
                        )
                        self.risk.record_entry(
                            market_id, price,
                            int(size / price) if price > 0 else 0,
                            side.lower(), size,
                        )
                        self._log_paper_trade(trade)
                    elif self._live_ready:
                        # LIVE: never execute here. Send a Telegram confirm
                        # request; execution happens only on explicit confirm,
                        # handled by _process_live_confirmations() next cycles.
                        if market_id in self._pending_live:
                            continue
                        sizing = {
                            "size_usd": size,
                            "shares": int(size / price) if price > 0 else 0,
                            "kelly_raw": opp.get("kelly_raw", 0),
                            "kelly_fractional": opp.get("kelly_fractional", 0),
                        }
                        try:
                            self.telegram.send_opportunity(opp, sizing)
                            self._pending_live.add(market_id)
                            logger.info(
                                f"[LIVE] Confirm requested via Telegram: {market_id} "
                                f"{side} ${size:.2f} @ {price:.3f}"
                            )
                        except Exception as e:
                            logger.error(f"[LIVE] Failed to send confirm request: {e}")
                    else:
                        # mode == "live" but preconditions failed: scan only.
                        logger.error(
                            "[LIVE] Refusing to trade — preconditions not met "
                            "(see startup CRITICAL logs). Scanning only."
                        )

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
