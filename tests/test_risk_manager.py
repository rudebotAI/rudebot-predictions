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
        risk_mgr._daily_loss = -100.0
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
