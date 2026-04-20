"""Tests for engines/resolution_sniper.py — near-certainty payout sniper."""
import time

from engines.resolution_sniper import DEFAULTS, ResolutionSniper


def _mkt(**overrides):
    base = {
        "market_id": "m1",
        "yes_price": 0.97,
        "no_price": 0.03,
        "resolves_at_epoch": time.time() + 30 * 60,  # 30 min out
        "depth_usd": 500.0,
        "title": "Test market",
    }
    base.update(overrides)
    return base


def _sniper(**obj):
    cfg = {"mode": "paper", "resolution_sniper": {"enabled": True, **obj}}
    return ResolutionSniper(cfg, deps={"fees": 0.0})


class TestDisabledByDefault:
    def test_default_disabled(self):
        assert DEFAULTS["enabled"] is False

    def test_disabled_returns_empty(self):
        s = ResolutionSniper({}, deps={})
        assert s.scan([_mkt()]) == []


class TestThresholds:
    def test_yes_above_threshold_accepted(self):
        s = _sniper()
        out = s.scan([_mkt(yes_price=0.97)])
        assert len(out) == 1
        assert out[0]["side"] == "YES"
        assert out[0]["price"] == 0.97

    def test_yes_just_below_threshold_rejected(self):
        s = _sniper()
        # Keep NO above its threshold too, so neither side qualifies
        assert s.scan([_mkt(yes_price=0.94, no_price=0.20)]) == []

    def test_yes_above_max_buy_price_rejected(self):
        s = _sniper(max_buy_price=0.98)
        # NO side also held above its threshold to isolate the YES cap check
        assert s.scan([_mkt(yes_price=0.99, no_price=0.20)]) == []

    def test_no_below_threshold_accepted(self):
        s = _sniper()
        out = s.scan([_mkt(yes_price=0.60, no_price=0.03)])
        assert len(out) == 1
        assert out[0]["side"] == "NO"


class TestTiming:
    def test_too_far_out_rejected(self):
        s = _sniper(max_minutes_to_resolution=60)
        assert s.scan([_mkt(resolves_at_epoch=time.time() + 3600 * 5)]) == []

    def test_too_close_rejected(self):
        # Within same-block race window
        s = _sniper(min_minutes_to_resolution=5)
        assert s.scan([_mkt(resolves_at_epoch=time.time() + 60)]) == []

    def test_missing_resolution_epoch_rejected(self):
        s = _sniper()
        m = _mkt()
        del m["resolves_at_epoch"]
        assert s.scan([m]) == []


class TestDepthFloor:
    def test_thin_market_rejected(self):
        s = _sniper(min_depth_usd=200.0)
        assert s.scan([_mkt(depth_usd=50.0)]) == []


class TestExpectedEdge:
    def test_edge_below_min_rejected(self):
        s = _sniper(min_expected_edge=0.05)
        # Buy YES at 0.97 → edge = 0.03, fails 0.05 requirement
        assert s.scan([_mkt(yes_price=0.97)]) == []

    def test_fees_eat_edge(self):
        s = ResolutionSniper(
            {"resolution_sniper": {"enabled": True, "min_expected_edge": 0.015}},
            deps={"fees": 0.02},  # 2% fee wipes the 3% cushion at 0.97
        )
        assert s.scan([_mkt(yes_price=0.97)]) == []


class TestSizeCapping:
    def test_size_capped_at_10_percent_of_depth(self):
        s = _sniper(max_usd_per_trade=1000.0)
        out = s.scan([_mkt(depth_usd=50.0 / 0.10)])  # depth = $500, cap is $50
        # depth 500, depth*0.1 = $50 but min_depth is 200 by default. Let's use $2000
        s2 = _sniper(max_usd_per_trade=1000.0)
        out = s2.scan([_mkt(depth_usd=2000.0)])
        assert len(out) == 1
        # Never take more than 10% of depth → $200
        assert out[0]["usd"] == 200.0

    def test_hard_cap_respected(self):
        s = _sniper(max_usd_per_trade=5.0)
        out = s.scan([_mkt(depth_usd=100000.0)])
        assert len(out) == 1
        assert out[0]["usd"] == 5.0
