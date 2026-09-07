"""
Main Loop -- Prediction Bot v6.0 (Kalshi-only)

Pipeline per scan cycle (6-stage framework):
  1. Risk     -- circuit breakers, drawdown, daily budget, exposure caps
  2. Scan     -- Kalshi events -> markets -> quotes
  3. Predict  -- EVScanner model probability, fee-adjusted EV, edge gate (>= 0.04)
  4. Size     -- fractional Kelly on the live bankroll, capped
  5. Execute  -- paper book, or Telegram-confirmed IOC order on Kalshi (V2 API)
  6. Compound -- closures book realized P&L into the risk manager's equity curve

v6 changes: V2 order endpoint + fill verification, Kelly actually wired,
edge threshold from config, NO-side P&L fixed (same-leg prices), live exit
path, record_exit wired (daily-loss / streak / drawdown breakers now fire),
Telegram commands handled in both modes, --once flag.
"""
import argparse
import json
import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import dashboard
from connectors.kalshi import KalshiConnector
from engines.scanner import EVScanner, parse_days_to_resolution
from research.calibration import CalibratedModel
from engines.sizing import KellySizer
from engines.cross_reference import enrich_with_cross_reference
from env_config import load_config
from execution.paper import PaperTrader
from execution.live import LiveTrader
from execution.resting import RestingOrders, own_leg_bid_ask
from alerts.telegram import TelegramAlerts
from risk_manager import RiskManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("predbot")

VERSION = "6.1"


def _to_float(x, default=0.0):
    if x is None:
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


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
        rc = self.config.risk
        # Predict stage: calibration model fitted on Kalshi's settled markets
        # (research/calibration.py). Missing file => no edge, bot idles safely.
        self.model = CalibratedModel.load()
        n_cells = self.model.model.get("n_cells_used", 0) if self.model.model else 0
        logger.info("calibration model: %s (%d usable cells, generated %s)",
                    "loaded" if self.model.model else "MISSING -> no edge source",
                    n_cells, self.model.model.get("generated", "-") if self.model.model else "-")
        self.scanner = EVScanner({
            "min_ev_threshold": rc.min_ev_threshold,
            "min_market_volume": 100,
            "max_days_to_resolution": rc.max_days_to_resolution,
            "allow_heuristic": bool(getattr(rc, "allow_heuristic", False)),
            "entry_style": rc.entry_style,
        }, model=self.model)
        self.sizer = KellySizer({
            "kelly_fraction": rc.kelly_fraction,
            "max_position_usd": rc.max_position_usd,
            "max_portfolio_pct": rc.max_portfolio_pct,
        })
        # Two ledgers: paper simulations and real fills never mix.
        self.paper = PaperTrader({"trade_log": "logs/trades.json",
                                  "performance_log": "logs/performance.json",
                                  "label": "PAPER"})
        self.live_book = PaperTrader({"trade_log": "logs/live_trades.json",
                                      "performance_log": "logs/live_performance.json",
                                      "label": "LIVE"})
        self.kalshi = KalshiConnector(
            self.config.kalshi.__dict__ if hasattr(self.config.kalshi, "__dict__") else {}
        )
        # Execute stage: post-only resting entries (paper sim + live), one ledger.
        self.resting = RestingOrders("logs/resting_orders.json")

        # Telegram confirm/alert channel + live executor.
        self.telegram = TelegramAlerts({
            "bot_token": self.config.telegram.bot_token,
            "chat_id": self.config.telegram.chat_id,
            "require_confirm": getattr(self.config.telegram, "require_confirm", True),
        })
        self.risk.on_halt = self._on_risk_halt
        self.live = LiveTrader(self.kalshi, self.risk, {
            "max_spread": rc.max_spread,
            "slippage": rc.slippage,
        })
        self._live_ready = False
        self._live_balance = None
        if self.config.mode == "live":
            self._live_ready = self._preflight_live()

        self._scan_number = 0
        self._last_scan_at = None
        self._last_kalshi_count = 0
        self._last_poly_count = 0
        self._last_ev_count = 0
        self._recent_signals = deque(maxlen=50)
        self._errors = deque(maxlen=20)
        # Market IDs with an outstanding live confirm request (avoid re-nagging).
        self._pending_live = set()

        logger.info(f"Bot v{VERSION} initialized in {self.config.mode} mode")
        logger.info(
            f"Risk limits: max_daily=${rc.max_daily_loss_usd}, max_pos=${rc.max_position_usd}, "
            f"max_event=${rc.max_event_exposure_usd}, MDD={rc.max_drawdown_pct:.0%}, "
            f"kelly={rc.kelly_fraction}, bankroll=${self.bankroll:.2f}"
        )
        logger.info(f"Gates: min_edge={rc.min_edge:.3f}, min_ev={rc.min_ev_threshold}")

    # ------------------------------------------------------------------
    @property
    def book(self) -> PaperTrader:
        return self.live_book if self.config.mode == "live" else self.paper

    @property
    def bankroll(self) -> float:
        """Sizing base. Live: exchange balance (refreshed each scan). Paper:
        configured bankroll compounded with realized P&L."""
        if self.config.mode == "live" and self._live_balance is not None:
            return float(self._live_balance)
        return max(0.0, self.risk.equity)

    def _refresh_live_balance(self):
        if not (self.config.mode == "live" and self._live_ready):
            return
        bal = self.kalshi.get_balance()
        if bal is not None:
            self._live_balance = bal
            self.risk.set_starting_equity(bal)

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
        else:
            bal = self.kalshi.get_balance()
            if bal is None:
                problems.append("Kalshi balance check failed (auth rejected or API unreachable)")
            else:
                self._live_balance = bal
                self.risk.set_starting_equity(bal)
                logger.info(f"[LIVE] Kalshi balance: ${bal:.2f}")

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

    # ------------------------------------------------------------------
    # Telegram: confirmations + commands
    # ------------------------------------------------------------------
    def _poll_telegram(self):
        if not self.telegram.is_configured():
            return
        try:
            confirmed = self.telegram.poll_callbacks()
        except Exception as e:
            logger.error(f"Telegram poll failed: {e}")
            return

        for cmd in self.telegram.drain_commands():
            self._handle_command(cmd)
        for info in self.telegram.drain_skipped():
            # Skipped markets may be re-offered on a later scan (at a fresh price).
            self._pending_live.discard(info.get("opp", {}).get("market_id"))

        if not (self.config.mode == "live" and self._live_ready):
            if confirmed:
                logger.warning(f"Ignoring {len(confirmed)} confirm(s): live trading not armed")
            return

        for info in confirmed:
            opp = info.get("opp", {})
            size_usd = float(info.get("sizing", {}).get("size_usd", 0) or 0)
            market_id = opp.get("market_id", "unknown")
            # Confirmation consumed; allow this market to be re-offered later.
            self._pending_live.discard(market_id)
            rc = self.config.risk
            if rc.entry_style == "maker":
                ok, why = self._recheck_edge(opp, rc)
                if not ok:
                    logger.warning(f"[LIVE] confirm for {market_id} dropped: {why}")
                    try:
                        self.telegram.send(f"<b>Not placed</b> -- {market_id}: {why}")
                    except Exception:
                        pass
                    continue
                result = self.live.execute_maker(opp, size_usd, rc.rest_seconds, nonce=f"{self._scan_number}")
                if result.get("success"):
                    self.resting.place(opp, result["price"], result["contracts"], "live", rc.rest_seconds,
                                       order_id=result.get("order_id"), client_order_id=result.get("client_order_id"))
                    try:
                        self.telegram.send(f"<b>Resting</b> {opp.get('signal')} {result['contracts']}x @ "
                                           f"{result['price']:.2f} on {market_id} (post-only, {rc.rest_seconds}s)")
                    except Exception:
                        pass
                else:
                    logger.warning(f"[LIVE] resting order not placed for {market_id}: {result.get('error')}")
                    try:
                        self.telegram.send(f"<b>Not placed</b> -- {market_id}: {result.get('error')}")
                    except Exception:
                        pass
                continue
            result = self.live.execute(opp, size_usd, nonce=f"{self._scan_number}")
            if result.get("success"):
                trade = self.live_book.open_position(opp, size_usd, fill=result)
                logger.info(f"[LIVE] Executed {market_id}: {result.get('contracts')} @ {result.get('price')}")
                try:
                    self.telegram.send_trade_opened({**trade, "status": "live"})
                except Exception as e:
                    logger.warning(f"[LIVE] fill notification failed: {e}")
            else:
                logger.warning(f"[LIVE] Order not placed for {market_id}: {result.get('error')}")
                try:
                    self.telegram.send(f"<b>Not filled</b> -- {market_id}: {result.get('error')}")
                except Exception:
                    pass

    def _on_risk_halt(self, reason: str):
        if not self.telegram.is_configured():
            return
        st = self.risk.get_status()
        self.telegram.send_risk_alert({
            "reason": reason,
            "daily_pnl": st.get("daily_pnl", 0),
            "consecutive_losses": st.get("consecutive_losses", 0),
        })

    def _handle_command(self, cmd: str):
        try:
            if cmd == "pnl":
                self.telegram.send_performance(self.book.get_performance(), self.risk.get_status())
            elif cmd == "status":
                st = self.risk.get_status()
                lines = [f"<b>Status</b> v{VERSION} ({self.config.mode})"]
                for k in ("halted", "halt_reason", "equity", "peak_equity", "drawdown_pct",
                          "sharpe", "daily_loss", "total_pnl", "open_positions",
                          "consecutive_losses", "closed_trades", "win_rate"):
                    lines.append(f"{k}: {st.get(k)}")
                lines.append(f"bankroll: ${self.bankroll:.2f}")
                lines.append(f"last scan: {self._last_scan_at} (#{self._scan_number})")
                self.telegram.send("\n".join(lines))
            elif cmd == "positions":
                pos = self.book.get_open_positions()
                if not pos:
                    self.telegram.send("No open positions")
                else:
                    lines = [f"<b>Open positions</b> ({len(pos)})"]
                    for p in pos:
                        lines.append(
                            f"{p.get('signal')} {p.get('question','')[:40]} @ {p.get('entry_price')} "
                            f"x{p.get('shares')} (${p.get('size_usd', 0):.2f})"
                        )
                    self.telegram.send("\n".join(lines))
            elif cmd == "resume":
                self.telegram.send(self.risk.force_resume())
        except Exception as e:
            logger.error(f"command /{cmd} failed: {e}")

    # ------------------------------------------------------------------
    def _build_state(self) -> dict:
        perf = self.book.get_performance()
        risk_status = "Active"
        rs = {}
        try:
            rs = self.risk.get_status()
            if isinstance(rs, dict) and rs.get("halted"):
                risk_status = f"Halted: {rs.get('halt_reason', '')}"
        except Exception as e:
            logger.debug(f"risk status unavailable: {e}")

        open_positions = self.book.get_open_positions()
        closed = self.book.get_closed_positions()
        closed.sort(key=lambda t: t.get("closed_at", ""), reverse=True)

        return {
            "mode": self.config.mode,
            "version": VERSION,
            "bankroll": round(self.bankroll, 2),
            "equity": rs.get("equity"),
            "drawdown_pct": rs.get("drawdown_pct"),
            "sharpe": rs.get("sharpe"),
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
            "resting_orders": self.resting.active(),
            "resting_summary": self.resting.summary(),
            "entry_style": self.config.risk.entry_style,
            "model": {
                "loaded": bool(self.model.model),
                "generated": self.model.model.get("generated") if self.model.model else None,
                "n_rows": self.model.model.get("n_rows") if self.model.model else 0,
                "cells_used": [k for k, c in self.model.cells.items() if c.get("used") and not k.startswith("ALL|")],
            },
            "calibration": self._calibration_stats(),
        }

    # ------------------------------------------------------------------
    # Scan / predict / size
    # ------------------------------------------------------------------
    async def scan_markets(self):
        logger.info("Scanning markets...")
        markets = []
        k_count = 0

        if "kalshi" in self.config.platforms:
            try:
                k_markets = await asyncio.to_thread(self.kalshi.scan_markets_with_prices, 50)
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

        if not markets:
            logger.warning("No markets fetched from any platform")
            return []

        opportunities = self.scanner.scan(markets)

        filtered = []
        bogus = 0
        min_edge = self.config.risk.min_edge
        for opp in opportunities:
            edge = opp.get("edge", 0)
            ev = opp.get("ev", 0)
            if ev >= EV_SANITY_CAP:
                bogus += 1
                continue
            if edge >= min_edge and ev > self.config.risk.min_ev_threshold:
                self._size_opportunity(opp)
                if opp["size_usd"] <= 0:
                    continue
                filtered.append(opp)
                logger.info(
                    f"Opportunity: {opp.get('question','unknown')[:50]} | "
                    f"EV={ev:.4f} | Edge={edge:.4f} | Kelly ${opp['size_usd']:.2f}"
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
                "size_usd": opp.get("size_usd", 0),
                "kelly_fractional": opp.get("kelly_fractional", 0),
                "platform": opp.get("platform", ""),
            })

        if not filtered:
            logger.info("No +EV opportunities found this scan cycle")
        return filtered

    def _size_opportunity(self, opp: dict) -> dict:
        """Fractional Kelly on the current bankroll; writes sizing into opp."""
        prob = float(opp.get("model_prob", 0) or 0)
        price = float(opp.get("market_price", 0) or 0)
        sizing = self.sizer.compute_size(prob, price, self.bankroll)
        opp["size_usd"] = sizing["size_usd"]
        opp["shares"] = sizing["shares"]
        opp["kelly_raw"] = sizing["kelly_raw"]
        opp["kelly_fractional"] = sizing["kelly_fractional"]
        return sizing

    # ------------------------------------------------------------------
    # Closures (stage 6: compound)
    # ------------------------------------------------------------------
    def _own_leg_price(self, signal: str, m: dict):
        """Current mark in the position's own leg (NO = 1 - yes mid)."""
        q = self.kalshi.quote(m)
        mid = q["mid"]
        if not mid:
            return None
        return mid if signal == "YES" else 1.0 - mid

    @staticmethod
    def kalshi_fee(price: float, contracts: float) -> float:
        """Kalshi taker fee: 0.07 * P * (1 - P) per contract (settlement is free)."""
        return round(0.07 * price * (1.0 - price) * contracts, 4)

    def _close(self, pos: dict, exit_price: float, reason: str):
        """Close a position in the active ledger, executing a real sell first in live mode."""
        market_id = pos.get("market_id", "")
        fees = 0.0
        if self.config.mode == "paper" and reason != "resolved":
            fees = self.kalshi_fee(exit_price, float(pos.get("shares") or 0))
        if self.config.mode == "live" and reason != "resolved":
            if not self._live_ready:
                return False
            res = self.live.close(pos, reason=reason, nonce=f"{self._scan_number}")
            if not res.get("success"):
                logger.warning(f"[LIVE] exit failed for {market_id}: {res.get('error')}")
                return False
            exit_price = float(res["price"])
            fees = float(res.get("fee_per_contract", 0) or 0) * float(res["contracts"])
            if res.get("partial"):
                # Partial exit: book the realized slice now, keep the remainder open.
                closed = self.book.close_position(pos["id"], exit_price, reason=reason,
                                                  shares=float(res["contracts"]), fees_usd=fees)
                if closed:
                    self.risk.record_partial_exit(market_id, float(closed.get("pnl", 0)),
                                                  float(closed.get("size_usd", 0)))
                return True
        closed = self.book.close_position(pos["id"], exit_price, reason=reason, fees_usd=fees)
        if not closed:
            return False
        self.risk.record_exit(market_id, float(closed.get("pnl", 0)))
        try:
            if self.telegram.is_configured():
                self.telegram.send_trade_closed(closed)
        except Exception as e:
            logger.debug(f"trade-closed notify failed: {e}")
        return True

    # ------------------------------------------------------------------
    # Execute stage: post-only resting entries
    # ------------------------------------------------------------------
    def _paper_rest(self, opp: dict, size: float, side: str, rc) -> bool:
        """Paper: rest at our side's bid. Nothing is booked until a later poll
        shows the market traded through our price (conservative fill model)."""
        market_id = opp.get("market_id", "")
        if self.resting.has_market(market_id, "paper"):
            return False
        yes_bid, yes_ask = _to_float(opp.get("yes_bid")), _to_float(opp.get("yes_ask"))
        bid, ask = own_leg_bid_ask(side, yes_bid, yes_ask)
        if bid <= 0.0 or bid >= 1.0:
            self.book.skip_opportunity(opp, "no bid to join")
            return False
        spread = _to_float(opp.get("spread"))
        if spread and spread > rc.max_spread:
            self.book.skip_opportunity(opp, f"spread {spread:.3f} > {rc.max_spread}")
            return False
        # Edge is re-checked at OUR price (better than the mid the scanner used).
        model_prob = float(opp.get("model_prob") or 0)
        if model_prob - bid < rc.min_edge:
            self.book.skip_opportunity(opp, "edge below gate at resting price")
            return False
        contracts = int(size / bid)
        if contracts < 1:
            self.book.skip_opportunity(opp, "size < 1 contract")
            return False
        self.resting.place(opp, bid, contracts, "paper", rc.rest_seconds)
        self.risk.record_entry(market_id, bid, contracts, side.lower(), contracts * bid,
                               event_ticker=opp.get("event_ticker"))
        return True

    def _poll_resting(self):
        """Advance every resting order: fill, partial, expire or cancel."""
        rc = self.config.risk
        for row in self.resting.active():
            market_id = row.get("market_id", "")
            mode = row.get("mode")
            try:
                if mode == "paper":
                    self._poll_paper_rest(row, rc)
                elif mode == "live" and self._live_ready:
                    self._poll_live_rest(row, rc)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"resting poll failed for {market_id}: {e}")

    def _book_rest_fill(self, row: dict, book, note: str, status: str = "filled",
                        filled: float | None = None, avg_fill: float | None = None,
                        fee_per_contract: float = 0.0):
        """Book a fill into the position book FIRST, then mark the ledger row —
        so a crash between the two leaves a row that reconciliation re-books
        (idempotent on order id), never a filled order with no position."""
        oid = row.get("order_id") or row["id"]
        already = any((t.get("order_id") == oid) for t in book.get_open_positions() + book.get_closed_positions())
        row_fill = dict(row)
        row_fill["filled"] = float(filled if filled is not None else row.get("filled") or row["contracts"])
        row_fill["avg_fill"] = avg_fill if avg_fill is not None else row.get("avg_fill")
        row_fill["fee_per_contract"] = fee_per_contract
        fill = self.resting.fill_for_book(row_fill)
        if not already:
            opp = dict(row.get("opp") or {})
            opp["market_price"] = fill["price"]
            opp["signal"] = row["signal"]
            trade = book.open_position(opp, fill["size_usd"], fill=fill)
            self.risk.resize_entry(row["market_id"], fill["price"], fill["contracts"], fill["size_usd"])
            logger.info(f"[{row['mode'].upper()}] resting order FILLED {row['signal']} {fill['contracts']:g}x "
                        f"@ {fill['price']:.4f} on {row['market_id']} ({note})")
            try:
                if self.telegram.is_configured():
                    self.telegram.send_trade_opened({**trade, "status": row["mode"]})
            except Exception as e:
                logger.debug(f"rest-fill notify failed: {e}")
        self.resting.settle(row, status, filled=row_fill["filled"], avg_fill=fill["price"],
                            fee_per_contract=fee_per_contract, note=note)

    def _reconcile_resting(self):
        """Startup / per-cycle: any ledger row marked filled/partial whose fill
        never reached the book (crash between book and settle in an older
        version, or between settle and book) is booked now, once."""
        for row in self.resting.recent(200):
            if row.get("status") not in ("filled", "partial") or row.get("reconciled"):
                continue
            book = self.paper if row.get("mode") == "paper" else self.live_book
            oid = row.get("order_id") or row["id"]
            if not any(t.get("order_id") == oid for t in book.get_open_positions() + book.get_closed_positions()):
                logger.warning(f"reconcile: booking orphaned fill {oid} on {row.get('market_id')}")
                self._book_rest_fill(row, book, "reconciled", status=row["status"])
            row["reconciled"] = True
        self.resting._save()

    def _poll_paper_rest(self, row: dict, rc):
        expired = time.time() >= float(row["expires_ts"])
        m = None
        try:
            m = self.kalshi.get_market(row["market_id"])
        except Exception as e:  # noqa: BLE001
            logger.debug(f"resting poll: get_market failed {row['market_id']}: {e}")
        if not m:
            if expired:
                self.resting.settle(row, "expired", note="expired (market unreadable)")
                self.risk.release_entry(row["market_id"])
            return
        q = self.kalshi.quote(m)
        status = (m.get("status") or "").lower()
        if not expired and status not in ("closed", "settled", "finalized") \
                and self.resting.paper_would_fill(row, q["yes_bid"], q["yes_ask"]):
            px = float(row["price"])
            maker_rate = (row.get("opp") or {}).get("maker_fee_rate")
            maker_rate = rc.maker_fee_rate if maker_rate is None else float(maker_rate)
            self._book_rest_fill(row, self.paper, "paper: market traded through our price",
                                 filled=row["contracts"], avg_fill=px,
                                 fee_per_contract=maker_rate * px * (1 - px))
        elif expired or status in ("closed", "settled", "finalized"):
            self.resting.settle(row, "expired", note="no fill before expiry/close")
            self.risk.release_entry(row["market_id"])
            logger.info(f"[PAPER] resting order expired unfilled on {row['market_id']}")

    def _poll_live_rest(self, row: dict, rc):
        expired = time.time() >= float(row["expires_ts"])
        st = None
        try:
            st = self.live.poll_order(row["order_id"]) if row.get("order_id") else None
        except Exception as e:  # noqa: BLE001
            logger.debug(f"resting poll: get_order failed {row.get('order_id')}: {e}")
        if not st:
            if expired and row.get("order_id"):
                # Cannot read it: try to kill it; release only once it is confirmed dead.
                if self.live.cancel(row["order_id"], row["market_id"]):
                    self.resting.settle(row, "cancelled", note="unreadable; cancelled at expiry")
                    self.risk.release_entry(row["market_id"])
            return
        filled = float(st.get("fill_count") or 0)
        px = float(st.get("average_fill_price") or row["price"])
        if row["signal"] == "NO" and st.get("average_fill_price"):
            px = 1.0 - float(st["average_fill_price"])          # exchange reports the yes leg
        fee = float(st.get("fee_per_contract") or 0.0)
        if st["status"] == "executed" or filled >= row["contracts"] - 1e-9:
            self._book_rest_fill(row, self.live_book, "live: exchange executed",
                                 filled=filled or row["contracts"], avg_fill=px, fee_per_contract=fee)
        elif st["status"] in ("canceled", "cancelled", "expired") or expired:
            if expired and st["status"] == "resting":
                if not self.live.cancel(row["order_id"], row["market_id"]):
                    return                                          # still alive: try again next cycle
            if filled > 0:
                self._book_rest_fill(row, self.live_book, "live: partial fill then cancelled",
                                     status="partial", filled=filled, avg_fill=px, fee_per_contract=fee)
            else:
                self.resting.settle(row, "expired", note="cancelled unfilled")
                self.risk.release_entry(row["market_id"])

    def _recheck_edge(self, opp: dict, rc) -> tuple:
        """A Telegram confirm can arrive minutes after the scan: re-run the
        model on a FRESH quote and require the edge at our resting bid."""
        try:
            m = self.kalshi.get_market(opp.get("market_id", ""))
            if not m:
                return False, "no fresh quote"
            q = self.kalshi.quote(m)
            side = str(opp.get("signal", "YES")).upper()
            bid, _ = own_leg_bid_ask(side, q["yes_bid"], q["yes_ask"])
            if bid <= 0:
                return False, "no bid to join"
            mid = q["mid"]
            hours = max(0.25, (parse_days_to_resolution(m.get("expected_expiration_time") or m.get("close_time")) or 0) * 24)
            p_yes = self.model.prob(mid, opp.get("category"), hours)
            p_side = p_yes if side == "YES" else 1.0 - p_yes
            if p_side - bid < rc.min_edge:
                return False, f"edge {p_side - bid:.3f} at bid {bid:.2f} below {rc.min_edge}"
            opp["model_prob"] = round(p_side, 4)
            return True, "ok"
        except Exception as e:  # noqa: BLE001
            return False, f"recheck failed: {e}"

    def _announce_model_state(self):
        """Telegram the operator whenever the set of USABLE calibration cells
        changes (a weekly refit commits models/calibration.json and Railway
        redeploys, so boot is the moment). Persisted in logs/model_state.json
        so an unchanged model on a plain restart stays quiet."""
        state_path = Path("logs/model_state.json")
        cells = sorted(k for k, c in self.model.cells.items() if c.get("used") and not k.startswith("ALL|"))
        generated = self.model.model.get("generated") if self.model.model else None
        prev = {}
        try:
            if state_path.exists():
                prev = json.loads(state_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            prev = {}
        changed = (prev.get("cells") != cells) or (prev.get("generated") != generated)
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({"cells": cells, "generated": generated,
                                              "version": VERSION, "mode": self.config.mode}), encoding="utf-8")
        except OSError as e:
            logger.debug(f"model_state save failed: {e}")
        if not changed or not self.telegram.is_configured():
            return
        if not self.model.model:
            text = (f"<b>Model MISSING</b> (v{VERSION}, {self.config.mode}) -- no calibration file; "
                    f"the bot scans but cannot find edge.")
        elif cells:
            lines = []
            for k in cells:
                c = self.model.cells[k]
                lines.append(f"  {k}: slope {c['b']:.2f}, n={c['n']}, Brier {c['brier_market_holdout']}->{c['brier_calibrated_holdout']}")
            text = (f"<b>Model ARMED</b> (v{VERSION}, {self.config.mode}) -- refit {str(generated)[:16]}: "
                    f"{len(cells)} usable cell(s). Trade cards can now fire.\n" + "\n".join(lines))
        else:
            text = (f"<b>Model refit -- no usable cells</b> (v{VERSION}, {self.config.mode}) -- refit "
                    f"{str(generated)[:16]} on {self.model.model.get('n_rows', 0)} settled markets. "
                    f"Bot stays idle: no edge source, no trades.")
        try:
            self.telegram.send(text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"model-state notify failed: {e}")

    def _calibration_stats(self) -> dict:
        """Brier score of the Predict stage on resolved positions vs the
        market's own Brier at entry — the honest 'did the model add anything'."""
        try:
            path = Path("logs/calibration.jsonl")
            if not path.exists():
                return {"n": 0}
            rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
            rows = [r for r in rows if r.get("model_prob") is not None and r.get("market_price") is not None]
            if not rows:
                return {"n": 0}
            bm = sum((float(r["model_prob"]) - r["y"]) ** 2 for r in rows) / len(rows)
            bp = sum((float(r["market_price"]) - r["y"]) ** 2 for r in rows) / len(rows)
            return {"n": len(rows), "brier_model": round(bm, 4), "brier_market": round(bp, 4),
                    "hit_rate": round(sum(r["y"] for r in rows) / len(rows), 3)}
        except Exception as e:  # noqa: BLE001
            logger.debug(f"calibration stats failed: {e}")
            return {"n": 0}

    def _log_calibration(self, pos: dict, result: str):
        """Append (model_prob, price, outcome) for every resolved position so
        the Predict stage is scored on Brier/reliability, not just P&L."""
        try:
            signal = (pos.get("signal") or "YES").upper()
            y = 1 if ((signal == "YES") == (result == "yes")) else 0
            row = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "market_id": pos.get("market_id"), "signal": signal,
                "category": pos.get("category"), "cell": pos.get("model_cell"),
                "model_prob": pos.get("model_prob"), "market_price": pos.get("entry_price"),
                "y": y, "mode": self.config.mode,
            }
            with open("logs/calibration.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"calibration log failed: {e}")

    def _check_closures(self):
        open_positions = self.book.get_open_positions()
        if not open_positions:
            return

        rc = self.config.risk
        closed_now = 0
        for pos in open_positions:
            market_id = pos.get("market_id", "")
            if not market_id or pos.get("platform", "kalshi") != "kalshi":
                continue

            try:
                m = self.kalshi.get_market(market_id)
            except Exception as e:
                logger.debug(f"closures: kalshi get_market({market_id}) failed: {e}")
                continue
            if not m:
                continue

            status = (m.get("status") or "").lower()
            result = (m.get("result") or "").lower()
            signal = (pos.get("signal") or "YES").upper()

            if status in ("finalized", "settled", "closed") and result in ("yes", "no"):
                win = (signal == "YES" and result == "yes") or (signal == "NO" and result == "no")
                if self._close(pos, 1.0 if win else 0.0, "resolved"):
                    closed_now += 1
                    self._log_calibration(pos, result)
                continue

            mark = self._own_leg_price(signal, m)
            entry = float(pos.get("entry_price") or 0)
            if not mark or entry <= 0:
                continue
            pnl_pct = (mark - entry) / entry

            if pnl_pct <= rc.stop_loss_pct:
                if self._close(pos, mark, "stop_loss"):
                    closed_now += 1
            elif pnl_pct >= rc.take_profit_pct:
                if self._close(pos, mark, "take_profit"):
                    closed_now += 1

        if closed_now:
            logger.info(f"closures: closed {closed_now} position(s)")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def run_cycle(self):
        self._scan_number += 1
        self._last_scan_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        self._refresh_live_balance()

        try:
            self._check_closures()
        except Exception as e:
            msg = f"closures error: {e}"
            logger.error(msg, exc_info=True)
            self._errors.append(msg)

        # Execute confirmed trades / handle commands before scanning again.
        self._poll_telegram()
        self._reconcile_resting()
        self._poll_resting()

        opportunities = await self.scan_markets()
        rc = self.config.risk
        for opp in opportunities:
            market_id = opp.get("market_id", "unknown")
            size = float(opp.get("size_usd", 0) or 0)
            side = opp.get("signal", "YES").upper()
            price = float(opp.get("market_price", 0) or 0)

            allowed, reason = self.risk.can_trade(
                market_id, size, side.lower(), event_ticker=opp.get("event_ticker")
            )
            if not allowed:
                logger.info(f"Skipping {market_id}: {reason}")
                self.book.skip_opportunity(opp, reason)
                continue

            if self.config.mode == "paper":
                if rc.entry_style == "maker":
                    self._paper_rest(opp, size, side, rc)
                    continue
                # TAKER (legacy v6): paper fills at the touch (ask), not the mid.
                fill_price = price
                yes_ask, yes_bid = _to_float(opp.get("yes_ask")), _to_float(opp.get("yes_bid"))
                if side == "YES" and yes_ask > 0:
                    fill_price = yes_ask
                elif side == "NO" and yes_bid > 0:
                    fill_price = 1.0 - yes_bid
                if fill_price <= 0 or fill_price >= 1:
                    continue
                contracts = int(size / fill_price)
                if contracts < 1:
                    self.book.skip_opportunity(opp, "size < 1 contract")
                    continue
                actual = contracts * fill_price
                trade = self.paper.open_position(opp, actual, fill={
                    "price": fill_price, "contracts": contracts, "size_usd": actual,
                    "fee_per_contract": self.kalshi_fee(fill_price, 1),
                })
                logger.info(f"[PAPER] Trade {market_id} {side} {contracts}x @ {fill_price:.4f} (${actual:.2f})")
                self.risk.record_entry(market_id, fill_price, contracts, side.lower(), actual,
                                       event_ticker=opp.get("event_ticker"))
                try:
                    if self.telegram.is_configured():
                        self.telegram.send_trade_opened(trade)
                except Exception as e:
                    logger.debug(f"paper notify failed: {e}")
            elif self._live_ready:
                # LIVE: never execute here. Send a Telegram confirm request;
                # execution happens only on explicit confirm (next cycle).
                if market_id in self._pending_live:
                    continue
                sizing = {
                    "size_usd": size,
                    "shares": opp.get("shares", 0),
                    "kelly_raw": opp.get("kelly_raw", 0),
                    "kelly_fractional": opp.get("kelly_fractional", 0),
                }
                try:
                    self.telegram.send_opportunity(opp, sizing)
                    self._pending_live.add(market_id)
                    logger.info(
                        f"[LIVE] Confirm requested via Telegram: {market_id} "
                        f"{side} ${size:.2f} @ {price:.4f}"
                    )
                except Exception as e:
                    logger.error(f"[LIVE] Failed to send confirm request: {e}")
            else:
                logger.error(
                    "[LIVE] Refusing to trade — preconditions not met "
                    "(see startup CRITICAL logs). Scanning only."
                )

    async def run(self, once: bool = False):
        dashboard.set_state_provider(self._build_state)
        try:
            dashboard.start()
        except Exception as e:
            logger.warning(f"dashboard failed to start: {e}")

        logger.info(f"Starting PredMarketBot v{VERSION} ({self.config.mode} mode)")
        logger.info(f"Scan interval: {self.config.scan_interval}s")
        self._announce_model_state()

        while True:
            try:
                await self.run_cycle()
                if once:
                    logger.info("--once: single cycle complete")
                    return
                await asyncio.sleep(self.config.scan_interval)
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                break
            except Exception as e:
                msg = f"Scan cycle error: {e}"
                logger.error(msg, exc_info=True)
                self._errors.append(msg)
                if once:
                    return
                await asyncio.sleep(self.config.scan_interval)


def _parse_args():
    ap = argparse.ArgumentParser(description="rudebot-predictions Kalshi bot")
    ap.add_argument("--once", action="store_true", help="run a single scan cycle and exit")
    return ap.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    bot = PredMarketBot()
    asyncio.run(bot.run(once=args.once))
