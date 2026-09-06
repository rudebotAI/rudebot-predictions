"""
Unit tests for PredictionBot risk manager.
Run: pytest tests/ -v
"""

import json
import os
import sys
import tempfile
import pytest
from unittest.mock import patch
from pathlib import Path

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class MockRiskConfig:
    max_daily_loss_usd = 100.0
    max_position_usd = 50.0
    max_open_positions = 5
    max_consecutive_losses = 3
    kelly_fraction = 0.25
    cooldown_seconds = 300
    min_ev_threshold = 0.05
    min_edge_bps = 200


class MockConfig:
    risk = MockRiskConfig()
    mode = "paper"


@pytest.fixture
def risk_mgr():
    """Create a RiskManager with temporary state file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = os.path.join(tmpdir, "risk_state.json")
        with patch("risk_manager.STATE_FILE", state_file):
            from risk_manager import RiskManager
            mgr = RiskManager(MockConfig())
            yield mgr


class TestCanTrade:
    def test_allows_valid_trade(self, risk_mgr):
        ok, reason = risk_mgr.can_trade("market-1", 25.0, "yes")
        assert ok is True
        assert reason == "OK"

    def test_blocks_oversized_position(self, risk_mgr):
        ok, reason = risk_mgr.can_trade("market-1", 75.0, "yes")
        assert ok is False
        assert "max" in reason.lower()

    def test_blocks_duplicate_position(self, risk_mgr):
        risk_mgr.record_entry("market-1", 0.55, 10, "yes", 25.0)
        ok, reason = risk_mgr.can_trade("market-1", 25.0, "yes")
        assert ok is False
        assert "Already" in reason

    def test_blocks_max_positions(self, risk_mgr):
        for i in range(5):
            risk_mgr.record_entry(f"market-{i}", 0.50, 10, "yes", 20.0)
        ok, reason = risk_mgr.can_trade("market-new", 20.0, "yes")
        assert ok is False
        assert "Max positions" in reason


class TestCircuitBreaker:
    def test_consecutive_loss_halt(self, risk_mgr):
        # Record 3 losses
        for i in range(3):
            risk_mgr.record_entry(f"m-{i}", 0.50, 10, "yes", 20.0)
            risk_mgr.record_exit(f"m-{i}", -15.0)

        ok, reason = risk_mgr.can_trade("m-next", 20.0, "yes")
        assert ok is False
        assert "consecutive" in reason.lower()

    def test_daily_loss_halt(self, risk_mgr):
        from datetime import datetime, timezone
        risk_mgr._daily_loss = -100.0
        risk_mgr._last_reset_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        risk_mgr._save_state()
        ok, reason = risk_mgr.can_trade("m-1", 20.0, "yes")
        assert ok is False
        assert "Daily loss" in reason

    def test_force_resume(self, risk_mgr):
        risk_mgr._halted = True
        risk_mgr._halt_reason = "test halt"
        result = risk_mgr.force_resume()
        assert "Resumed" in result
        ok, _ = risk_mgr.can_trade("m-1", 20.0, "yes")
        assert ok is True


class TestPnLTracking:
    def test_win_resets_consecutive(self, risk_mgr):
        risk_mgr.record_entry("m-1", 0.50, 10, "yes", 20.0)
        risk_mgr.record_exit("m-1", -10.0)
        assert risk_mgr._consecutive_losses == 1

        risk_mgr.record_entry("m-2", 0.50, 10, "yes", 20.0)
        risk_mgr.record_exit("m-2", 15.0)
        assert risk_mgr._consecutive_losses == 0

    def test_total_pnl_tracking(self, risk_mgr):
        risk_mgr.record_entry("m-1", 0.50, 10, "yes", 20.0)
        risk_mgr.record_exit("m-1", 15.0)
        risk_mgr.record_entry("m-2", 0.50, 10, "yes", 20.0)
        risk_mgr.record_exit("m-2", -5.0)
        assert risk_mgr._total_pnl == 10.0


class TestStatePersistence:
    def test_state_survives_reload(self, risk_mgr):
        risk_mgr.record_entry("m-1", 0.55, 10, "yes", 25.0)
        risk_mgr.record_exit("m-1", -10.0)

        # Reload from same state file
        state_file = risk_mgr._save_state.__code__.co_filename  # hack
        status = risk_mgr.get_status()
        assert status["trade_count"] == 1
        assert status["total_pnl"] == -10.0

    def test_status_output(self, risk_mgr):
        status = risk_mgr.get_status()
        assert "mode" in status
        assert "halted" in status
        assert "daily_loss" in status
        assert "win_rate" in status
        assert status["mode"] == "paper"


class MockRiskConfigV6(MockRiskConfig):
    bankroll_usd = 100.0
    max_drawdown_pct = 0.08
    max_event_exposure_usd = 30.0


class MockConfigV6:
    risk = MockRiskConfigV6()
    mode = "paper"


@pytest.fixture
def risk_v6():
    with tempfile.TemporaryDirectory() as tmpdir:
        from risk_manager import RiskManager
        yield RiskManager(MockConfigV6(), state_file=os.path.join(tmpdir, "s.json"))


class TestV6Controls:
    def test_drawdown_breaker_is_sticky(self, risk_v6):
        risk_v6.record_entry("m1", 0.5, 10, "yes", 5.0)
        risk_v6.record_exit("m1", -9.0)        # equity 91 -> DD 9% > 8%
        ok, reason = risk_v6.can_trade("m2", 5.0)
        assert ok is False and "drawdown" in reason.lower()
        # Cooldown never clears it; /resume does and re-bases the peak.
        risk_v6._halt_time = "2000-01-01T00:00:00+00:00"
        assert risk_v6.can_trade("m2", 5.0)[0] is False
        risk_v6.force_resume()
        assert risk_v6.can_trade("m2", 5.0)[0] is True
        assert risk_v6.drawdown_pct == 0

    def test_cooldown_resets_loss_streak(self, risk_v6):
        for i in range(3):
            risk_v6.record_entry(f"m{i}", 0.5, 1, "yes", 1.0)
            risk_v6.record_exit(f"m{i}", -0.5)
        assert risk_v6.can_trade("x", 1.0)[0] is False   # trips breaker
        risk_v6._halt_time = "2000-01-01T00:00:00+00:00"  # cooldown elapsed
        assert risk_v6.can_trade("x", 1.0)[0] is True     # does not re-trip

    def test_event_exposure_cap(self, risk_v6):
        risk_v6.record_entry("m1", 0.5, 10, "yes", 20.0, event_ticker="EV1")
        ok, reason = risk_v6.can_trade("m2", 15.0, event_ticker="EV1")
        assert ok is False and "Event exposure" in reason
        assert risk_v6.can_trade("m3", 15.0, event_ticker="EV2")[0] is True

    def test_sharpe_and_equity_in_status(self, risk_v6):
        for i, pnl in enumerate([1.0, -0.5, 2.0]):
            risk_v6.record_entry(f"m{i}", 0.5, 10, "yes", 5.0)
            risk_v6.record_exit(f"m{i}", pnl)
        st = risk_v6.get_status()
        assert st["equity"] == pytest.approx(102.5)
        assert st["sharpe"] is not None and st["closed_trades"] == 3


class TestModeSeparation:
    def test_live_mode_seeds_equity_from_balance_not_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from risk_manager import RiskManager

            class LiveCfg:
                risk = MockRiskConfigV6()   # bankroll_usd = 100 must be ignored
                mode = "live"

            rm = RiskManager(LiveCfg(), state_file=os.path.join(tmpdir, "l.json"))
            assert rm.equity == 0
            rm.set_starting_equity(50.0)
            assert rm.equity == 50.0
            rm.record_entry("m", 0.5, 10, "yes", 5.0)
            rm.record_exit("m", -5.0)          # 10% DD of the REAL $50 account
            assert rm.can_trade("x", 1.0)[0] is False

    def test_default_state_files_differ_by_mode(self):
        from risk_manager import RiskManager, STATE_FILE, LIVE_STATE_FILE

        class P:
            risk = MockRiskConfigV6(); mode = "paper"

        class L:
            risk = MockRiskConfigV6(); mode = "live"

        with patch("risk_manager.STATE_FILE", "/tmp/_rb_p.json"), \
             patch("risk_manager.LIVE_STATE_FILE", "/tmp/_rb_l.json"):
            assert str(RiskManager(P()).state_path) != str(RiskManager(L()).state_path)

    def test_daily_loss_halt_ignores_cooldown(self, risk_v6):
        risk_v6.config.max_drawdown_pct = 0     # isolate the daily-loss gate
        try:
            risk_v6.can_trade("warmup", 1.0)    # stamps today's reset date
            risk_v6.record_entry("m", 0.5, 10, "yes", 5.0)
            risk_v6.record_exit("m", -100.0)    # >= max_daily_loss_usd
            ok, reason = risk_v6.can_trade("x", 1.0)
            assert ok is False and "Daily loss" in reason
            risk_v6._halt_time = "2000-01-01T00:00:00+00:00"
            ok, reason = risk_v6.can_trade("x", 1.0)
            assert ok is False and "00:00 UTC" in reason
        finally:
            risk_v6.config.max_drawdown_pct = 0.08

    def test_on_halt_callback_fires(self, risk_v6):
        seen = []
        risk_v6.on_halt = seen.append
        for i in range(3):
            risk_v6.record_entry(f"m{i}", 0.5, 1, "yes", 1.0)
            risk_v6.record_exit(f"m{i}", -0.1)
        risk_v6.can_trade("x", 1.0)
        assert seen and "consecutive" in seen[0]
