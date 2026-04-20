"""Tests for engines/obi.py — orderbook imbalance fade strategy."""

from engines.obi import DEFAULTS, OBIStrategy


def _enabled_cfg(**overrides):
    cfg = {"obi": {"enabled": True, "min_top_depth_usd": 100.0, **overrides}}
    return cfg


class TestDisabledByDefault:
    def test_default_config_is_disabled(self):
        assert DEFAULTS["enabled"] is False

    def test_disabled_returns_none(self):
        s = OBIStrategy({})
        book = {"market_id": "m1",
                "bids": [[0.50, 1000]], "asks": [[0.51, 1000]]}
        assert s.evaluate(book) is None


class TestImbalanceDetection:
    def test_heavy_bid_fires_sell_signal(self):
        s = OBIStrategy(_enabled_cfg(threshold=0.60))
        # bids 800×0.50=$400, asks 100×0.51=$51 → OBI ≈ 0.89
        book = {
            "market_id": "m1",
            "bids": [[0.50, 800]],
            "asks": [[0.51, 100], [0.52, 100]],
        }
        sig = s.evaluate(book)
        assert sig is not None
        assert sig["side"] == "SELL"
        assert sig["kind"] == "obi_fade"
        assert sig["obi"] >= 0.60

    def test_heavy_ask_fires_buy_signal(self):
        s = OBIStrategy(_enabled_cfg(threshold=0.60))
        # asks 800×0.51=$408, bids 100×0.50=$50 → OBI ≈ 0.11 ≤ 0.40
        book = {
            "market_id": "m1",
            "bids": [[0.50, 100]],
            "asks": [[0.51, 800]],
        }
        # Need both sides >= min_top_depth ($100). Bids are only $50 → no signal.
        assert s.evaluate(book) is None

    def test_balanced_book_returns_none(self):
        s = OBIStrategy(_enabled_cfg(threshold=0.60))
        book = {
            "market_id": "m1",
            "bids": [[0.50, 500]],
            "asks": [[0.51, 500]],
        }
        assert s.evaluate(book) is None


class TestDepthFloor:
    def test_both_sides_must_have_min_depth(self):
        s = OBIStrategy(_enabled_cfg(min_top_depth_usd=1000.0))
        # Both sides have $500 each; both below the $1000 floor → skip
        book = {
            "market_id": "m1",
            "bids": [[0.50, 1000]],
            "asks": [[0.51, 1000]],
        }
        assert s.evaluate(book) is None


class TestEmptyBook:
    def test_empty_bids_returns_none(self):
        s = OBIStrategy(_enabled_cfg())
        assert s.evaluate({"bids": [], "asks": [[0.5, 100]]}) is None

    def test_empty_asks_returns_none(self):
        s = OBIStrategy(_enabled_cfg())
        assert s.evaluate({"bids": [[0.5, 100]], "asks": []}) is None


class TestTradeSizing:
    def test_signal_carries_max_usd_per_trade(self):
        s = OBIStrategy(_enabled_cfg(max_usd_per_trade=7.5, threshold=0.60))
        book = {
            "market_id": "m1",
            "bids": [[0.50, 800]],
            "asks": [[0.51, 100], [0.52, 100]],
        }
        sig = s.evaluate(book)
        assert sig is not None
        assert sig["usd"] == 7.5
