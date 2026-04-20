"""Tests for engines/copy_trader.py — wallet copy trading with EV+Kelly filter."""
from unittest.mock import MagicMock

from engines.copy_trader import DEFAULTS, CopyTrader, Delta


class _FakePoly:
    def __init__(self, positions_by_wallet):
        self._p = positions_by_wallet

    def get_positions(self, addr):
        return self._p.get(addr, [])


class _Scanner:
    def __init__(self, result):
        self._r = result

    def evaluate(self, **kwargs):
        return self._r


class _Sizer:
    def __init__(self, value):
        self._v = value

    def kelly_size(self, **kwargs):
        return self._v


def _cfg(**overrides):
    base = {
        "mode": "paper",
        "copy_trading": {
            "enabled": True,
            "wallets": ["0xabc"],
            "copy_percentage": 0.10,
            "max_copy_usd_per_trade": 10.0,
            "per_wallet_daily_cap_usd": 50.0,
            "min_ev_gap": 0.05,
            **overrides,
        },
    }
    return base


class TestDisabledByDefault:
    def test_default_disabled(self):
        assert DEFAULTS["enabled"] is False

    def test_disabled_returns_empty(self):
        ct = CopyTrader({"copy_trading": {"enabled": False, "wallets": ["0x1"]}}, deps={})
        assert ct.scan_once() == []


class TestEVFilter:
    def test_ev_gap_below_threshold_rejects_copy(self):
        poly = _FakePoly({"0xabc": [
            {"market_id": "m1", "side": "YES", "shares": 10, "avg_price": 0.50}
        ]})
        ct = CopyTrader(_cfg(min_ev_gap=0.10), deps={
            "polymarket": poly,
            "scanner": _Scanner({"ev_gap": 0.03, "true_prob": 0.55}),
            "sizer": _Sizer(5.0),
        })
        # First scan establishes baseline, returns an "opening" candidate.
        # But EV filter should reject.
        assert ct.scan_once() == []

    def test_ev_gap_above_threshold_accepts(self):
        poly = _FakePoly({"0xabc": [
            {"market_id": "m1", "side": "YES", "shares": 10, "avg_price": 0.50}
        ]})
        ct = CopyTrader(_cfg(min_ev_gap=0.05), deps={
            "polymarket": poly,
            "scanner": _Scanner({"ev_gap": 0.10, "true_prob": 0.60}),
            "sizer": _Sizer(5.0),
        })
        out = ct.scan_once()
        assert len(out) == 1
        assert out[0]["kind"] == "copy_open"
        assert out[0]["source"] == "copy_trader"


class TestSizingCaps:
    def test_per_trade_cap_enforced(self):
        poly = _FakePoly({"0xabc": [
            # Whale moves 1000 shares at $0.50 = $500 notional
            {"market_id": "m1", "side": "YES", "shares": 1000, "avg_price": 0.50}
        ]})
        ct = CopyTrader(
            _cfg(copy_percentage=0.50, max_copy_usd_per_trade=7.0),
            deps={
                "polymarket": poly,
                "scanner": _Scanner({"ev_gap": 0.10, "true_prob": 0.60}),
                "sizer": _Sizer(100.0),
            },
        )
        out = ct.scan_once()
        assert len(out) == 1
        assert out[0]["usd"] == 7.0  # hard cap wins

    def test_kelly_caps_below_copy_percentage(self):
        poly = _FakePoly({"0xabc": [
            {"market_id": "m1", "side": "YES", "shares": 1000, "avg_price": 0.50}
        ]})
        ct = CopyTrader(
            _cfg(copy_percentage=0.50, max_copy_usd_per_trade=1000.0),
            deps={
                "polymarket": poly,
                "scanner": _Scanner({"ev_gap": 0.10, "true_prob": 0.60}),
                "sizer": _Sizer(3.0),  # Kelly says only $3
            },
        )
        out = ct.scan_once()
        assert len(out) == 1
        assert out[0]["usd"] == 3.0  # Kelly wins

    def test_zero_kelly_rejects_when_required(self):
        poly = _FakePoly({"0xabc": [
            {"market_id": "m1", "side": "YES", "shares": 10, "avg_price": 0.50}
        ]})
        ct = CopyTrader(_cfg(), deps={
            "polymarket": poly,
            "scanner": _Scanner({"ev_gap": 0.10, "true_prob": 0.60}),
            "sizer": _Sizer(0.0),
        })
        assert ct.scan_once() == []


class TestDailyCap:
    def test_daily_cap_enforced_across_cycles(self):
        # Build two wallets worth of trades that each would hit the cap
        poly = _FakePoly({"0xabc": [
            {"market_id": "m1", "side": "YES", "shares": 1000, "avg_price": 0.50}
        ]})
        ct = CopyTrader(
            _cfg(per_wallet_daily_cap_usd=8.0,
                 max_copy_usd_per_trade=10.0,
                 copy_percentage=1.0),
            deps={
                "polymarket": poly,
                "scanner": _Scanner({"ev_gap": 0.10, "true_prob": 0.60}),
                "sizer": _Sizer(100.0),
            },
        )
        out = ct.scan_once()
        assert len(out) == 1
        # Copy USD should be capped at the daily cap
        assert out[0]["usd"] == 8.0


class TestCloseMirroring:
    def test_position_exit_emits_close_candidate(self):
        # Snapshot 1: wallet holds 10 shares
        # Snapshot 2: wallet has closed
        class StateMutatingPoly:
            def __init__(self):
                self.calls = 0
            def get_positions(self, addr):
                self.calls += 1
                if self.calls == 1:
                    return [{"market_id": "m1", "side": "YES",
                             "shares": 10, "avg_price": 0.50}]
                return []

        poly = StateMutatingPoly()
        ct = CopyTrader(_cfg(), deps={
            "polymarket": poly,
            "scanner": _Scanner({"ev_gap": 0.10, "true_prob": 0.60}),
            "sizer": _Sizer(5.0),
        })
        _ = ct.scan_once()   # seeds the open
        out = ct.scan_once()  # detects close
        assert len(out) == 1
        assert out[0]["kind"] == "copy_close"
