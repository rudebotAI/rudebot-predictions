"""
Telegram Alert System -- Sends trade signals and handles confirm/skip buttons.
Reuses patterns from the copy trade bot we built earlier.
"""

import json
import time
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

TG_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramAlerts:
    """Telegram bot for trade alerts and commands."""

    def __init__(self, config: dict):
        self.token = config.get("bot_token", "")
        self.chat_id = config.get("chat_id", "")
        self.require_confirm = config.get("require_confirm", True)
        self.offset = 0
        self.pending_confirms = {}  # callback_data -> opportunity+sizing
        self.confirmed = []   # Confirmed trade IDs
        self.skipped = []     # Skipped trade IDs
        self._api_reachable = None  # None=untested, True/False=cached

    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _post(self, method: str, data: dict) -> dict:
        if self._api_reachable is False:
            return {}
        url = TG_API.format(token=self.token, method=method)
        body = json.dumps(data).encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=3) as resp:
                self._api_reachable = True
                return json.loads(resp.read().decode())
        except Exception as e:
            if self._api_reachable is None:
                self._api_reachable = False
                logger.warning(f"Telegram API unreachable -- skipping: {e}")
            return {}

    def _get(self, method: str, params: str = "") -> dict:
        if self._api_reachable is False:
            return {}
        url = TG_API.format(token=self.token, method=method) + ("?" + params if params else "")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            })
            with urllib.request.urlopen(req, timeout=3) as resp:
                self._api_reachable = True
                return json.loads(resp.read().decode())
        except Exception as e:
            if self._api_reachable is None:
                self._api_reachable = False
                logger.warning(f"Telegram API unreachable -- skipping: {e}")
            return {}

    def send(self, text: str, reply_markup=None) -> dict:
        data = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            data["reply_markup"] = reply_markup
        return self._post("sendMessage", data)

    def send_opportunity(self, opp: dict, sizing: dict) -> str:
        """
        Send a trade opportunity alert with Confirm/Skip buttons.
        Returns callback_id for tracking.
        """
        cb_id = f"trade_{int(time.time())}_{hash(opp.get('question',''))%10000}"

        ev = opp.get("ev", 0)
        edge = opp.get("edge", 0)
        price = opp.get("market_price", 0)
        signal = opp.get("signal", "?")
        platform = opp.get("platform", "?")
        question = opp.get("question", "Unknown")[:80]

        msg = (
            f"<b>Signal Detected</b>\n"
            f"{'─' * 32}\n"
            f"<b>Market:</b> {question}\n"
            f"<b>Platform:</b> {platform.title()}\n"
            f"<b>Signal:</b> {signal} @ {price:.3f}\n"
            f"<b>EV:</b> {ev:.3f} | <b>Edge:</b> {edge:.3f}\n"
            f"{'─' * 32}\n"
            f"<b>Kelly says:</b> ${sizing.get('size_usd', 0):.2f} "
            f"({sizing.get('shares', 0):.1f} shares)\n"
            f"<b>Kelly raw:</b> {sizing.get('kelly_raw', 0):.3f} "
            f"-> frac: {sizing.get('kelly_fractional', 0):.3f}\n"
            f"{'─' * 32}\n"
        )

        # Research data if available
        research = opp.get("research")
        if research and research.get("sources_used", 0) > 0:
            msg += (
                f"<b>Research:</b> {research['direction']} "
                f"(LR={research['combined_lr']:.2f}, "
                f"{research['sources_used']} sources)\n"
                f"{'─' * 32}\n"
            )

        if self.require_confirm:
            msg += "<i>Tap to confirm or skip:</i>"
            markup = {
                "inline_keyboard": [[
                    {"text": "Confirm Trade", "callback_data": f"confirm_{cb_id}"},
                    {"text": "Skip", "callback_data": f"skip_{cb_id}"},
                ]]
            }
        else:
            msg += "<i>Auto-executing (confirm disabled)</i>"
            markup = None

        self.send(msg, markup)

        # Store for callback handling
        self.pending_confirms[f"confirm_{cb_id}"] = {"opp": opp, "sizing": sizing}
        self.pending_confirms[f"skip_{cb_id}"] = {"opp": opp, "sizing": sizing}

        return cb_id

    def send_trade_opened(self, trade: dict):
        """Notify that a trade was opened."""
        mode = "PAPER" if trade.get("status") == "open" else "LIVE"
        self.send(
            f"<b>{mode} Trade Opened</b>\n"
            f"{trade.get('signal', '?')} -- {trade.get('question', '?')[:60]}\n"
            f"Entry: {trade.get('entry_price', 0):.3f} | ${trade.get('size_usd', 0):.2f}"
        )

    def send_trade_closed(self, trade: dict):
        """Notify that a trade was closed."""
        pnl = trade.get("pnl", 0)
        emoji = "+" if pnl >= 0 else ""
        self.send(
            f"<b>Trade Closed</b>\n"
            f"{trade.get('question', '?')[:60]}\n"
            f"P&L: {emoji}${pnl:.2f} ({emoji}{trade.get('pnl_pct', 0):.1f}%)\n"
            f"Reason: {trade.get('close_reason', '?')}"
        )

    def send_risk_alert(self, status: dict):
        """Alert when risk manager halts trading."""
        self.send(
            f"<b>RISK ALERT</b>\n"
            f"{'─' * 32}\n"
            f"Trading halted: {status.get('reason', '?')}\n"
            f"Daily P&L: ${status.get('daily_pnl', 0):.2f}\n"
            f"Consecutive losses: {status.get('consecutive_losses', 0)}\n"
            f"{'─' * 32}\n"
            f"Send /resume to manually restart"
        )

    def send_performance(self, perf: dict, risk_status: dict):
        """Send performance summary."""
        self.send(
            f"<b>Performance Summary</b>\n"
            f"{'─' * 32}\n"
            f"Trades: {perf.get('total_trades', 0)} "
            f"(W:{perf.get('wins', 0)} L:{perf.get('losses', 0)})\n"
            f"Win rate: {perf.get('win_rate', 0):.1f}%\n"
            f"Total P&L: ${perf.get('total_pnl', 0):.2f}\n"
            f"Avg P&L: ${perf.get('avg_pnl', 0):.2f}\n"
            f"Best: ${perf.get('best_trade', 0):.2f} | "
            f"Worst: ${perf.get('worst_trade', 0):.2f}\n"
            f"Open: {perf.get('open_positions', 0)}\n"
            f"{'─' * 32}\n"
            f"Daily P&L: ${risk_status.get('daily_pnl', 0):.2f}\n"
            f"Status: {'HALTED' if risk_status.get('halted') else 'Active'}"
        )

    def send_scan_summary(self, poly_count: int, kalshi_count: int, ev_count: int, arb_count: int, div_count: int):
        """Send a summary after each scan cycle."""
        self.send(
            f"<b>Scan Complete</b>\n"
            f"{'─' * 32}\n"
            f"Polymarket: {poly_count} markets\n"
            f"Kalshi: {kalshi_count} markets\n"
            f"EV signals: {ev_count}\n"
            f"Arb signals: {arb_count}\n"
            f"KL-Div signals: {div_count}\n"
            f"{'─' * 32}\n"
            f"{time.strftime('%H:%M:%S')} -- next scan in 2 min"
        )

    def send_error(self, error_msg: str):
        """Alert on errors."""
        self.send(f"<b>Error</b>\n{error_msg[:200]}")

    def poll_callbacks(self) -> list:
        """
        Check for user button presses.
        Returns list of confirmed opportunities.
        """
        result = self._get("getUpdates", f"offset={self.offset}&timeout=2")
        if not result or not result.get("ok"):
            return []

        confirmed_trades = []

        for update in result.get("result", []):
            self.offset = update["update_id"] + 1

            # Handle button press
            cb = update.get("callback_query")
            if cb:
                data = cb.get("data", "")
                cb_query_id = cb["id"]
                msg_id = cb.get("message", {}).get("message_id")

                if data in self.pending_confirms:
                    info = self.pending_confirms.pop(data)
                    # Remove opposite button
                    opposite = data.replace("confirm_", "skip_") if "confirm_" in data else data.replace("skip_", "confirm_")
                    self.pending_confirms.pop(opposite, None)

                    if data.startswith("confirm_"):
                        confirmed_trades.append(info)
                        self._post("answerCallbackQuery", {"callback_query_id": cb_query_id, "text": "Trade confirmed!"})
                        if msg_id:
                            self._post("editMessageText", {
                                "chat_id": self.chat_id, "message_id": msg_id,
                                "text": f"<b>CONFIRMED</b> -- {info['opp'].get('question','')[:60]}",
                                "parse_mode": "HTML"
                            })
                    elif data.startswith("skip_"):
                        self._post("answerCallbackQuery", {"callback_query_id": cb_query_id, "text": "Skipped"})
                        if msg_id:
                            self._post("editMessageText", {
                                "chat_id": self.chat_id, "message_id": msg_id,
                                "text": f"<b>SKIPPED</b> -- {info['opp'].get('question','')[:60]}",
                                "parse_mode": "HTML"
                            })
                else:
                    self._post("answerCallbackQuery", {"callback_query_id": cb_query_id, "text": "Expired"})

            # Handle text commands
            message = update.get("message", {})
            text = (message.get("text") or "").strip().lower()
            if text == "/pnl":
                self.confirmed.append({"command": "pnl"})
            elif text == "/status":
                self.confirmed.append({"command": "status"})
            elif text == "/resume":
                self.confirmed.append({"command": "resume"})
            elif text == "/positions":
                self.confirmed.append({"command": "positions"})
            elif text == "/help":
                self.send(
                    "<b>Commands</b>\n"
                    "/pnl -- Performance summary\n"
                    "/status -- Risk & bot status\n"
                    "/positions -- Open positions\n"
                    "/resume -- Resume after halt\n"
                    "/help -- This message"
                )

        return confirmed_trades
