"""
Regression tests for alerts/telegram.py network resilience.

Bug history: a single transient timeout at boot set _api_reachable=False
permanently, silently dropping every live-trade confirm request while
"[LIVE] Confirm requested" kept logging. These tests pin the new behavior:
failures back off temporarily, always recover, and undelivered confirm
requests raise instead of passing silently.
"""

import time
import unittest
from unittest import mock

import alerts.telegram as tg
from alerts.telegram import TelegramAlerts


def make_alerts():
    return TelegramAlerts({"bot_token": "tok", "chat_id": "123"})


class FakeResp:
    def __init__(self, payload=b'{"ok": true, "result": {"message_id": 1}}'):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestFailureRecovery(unittest.TestCase):
    def test_failure_does_not_permanently_disable(self):
        """One timeout must NOT kill alerts forever -- they retry after cooldown."""
        alerts = make_alerts()

        with mock.patch("urllib.request.urlopen", side_effect=OSError("timed out")):
            self.assertEqual(alerts._post("sendMessage", {}), {})
        self.assertEqual(alerts._fail_count, 1)

        # Within cooldown: skipped, no network call.
        with mock.patch("urllib.request.urlopen") as m:
            self.assertEqual(alerts._post("sendMessage", {}), {})
            m.assert_not_called()

        # After cooldown: retried and recovers.
        alerts._last_fail_ts = time.monotonic() - (tg.RETRY_COOLDOWN + 1)
        with mock.patch("urllib.request.urlopen", return_value=FakeResp()):
            result = alerts._post("sendMessage", {})
        self.assertTrue(result.get("ok"))
        self.assertEqual(alerts._fail_count, 0)

    def test_every_failure_logs_warning(self):
        """Failures must never be silent -- each retry failure warns again."""
        alerts = make_alerts()
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            with self.assertLogs("alerts.telegram", level="WARNING"):
                alerts._post("sendMessage", {})
            alerts._last_fail_ts = time.monotonic() - (tg.RETRY_COOLDOWN + 1)
            with self.assertLogs("alerts.telegram", level="WARNING"):
                alerts._post("sendMessage", {})
        self.assertEqual(alerts._fail_count, 2)

    def test_get_uses_same_cooldown(self):
        alerts = make_alerts()
        with mock.patch("urllib.request.urlopen", side_effect=OSError("timed out")):
            alerts._get("getUpdates")
        with mock.patch("urllib.request.urlopen") as m:
            self.assertEqual(alerts._get("getUpdates"), {})
            m.assert_not_called()


class TestSendOpportunity(unittest.TestCase):
    OPP = {"question": "Will X happen?", "ev": 0.2, "edge": 0.05,
           "market_price": 0.5, "signal": "YES", "platform": "kalshi"}
    SIZING = {"size_usd": 2.0, "shares": 4, "kelly_raw": 0.1, "kelly_fractional": 0.025}

    def test_raises_when_not_delivered(self):
        """Undelivered confirm requests must raise, not silently no-op."""
        alerts = make_alerts()
        with mock.patch.object(alerts, "send", return_value={}):
            with self.assertRaises(RuntimeError):
                alerts.send_opportunity(self.OPP, self.SIZING)
        self.assertEqual(alerts.pending_confirms, {})

    def test_registers_pending_on_success(self):
        alerts = make_alerts()
        with mock.patch.object(alerts, "send", return_value={"ok": True, "result": {}}):
            cb_id = alerts.send_opportunity(self.OPP, self.SIZING)
        self.assertIn(f"confirm_{cb_id}", alerts.pending_confirms)
        self.assertIn(f"skip_{cb_id}", alerts.pending_confirms)


if __name__ == "__main__":
    unittest.main()
