"""Tests for engines/polygon_whale.py — on-chain whale signal watcher."""
import asyncio

import pytest

from engines.polygon_whale import (
    DEFAULTS,
    POLYMARKET_CLOB_EXCHANGE,
    PolygonWhaleWatcher,
    WhaleSignal,
)


class TestDisabledByDefault:
    def test_default_disabled(self):
        assert DEFAULTS["enabled"] is False

    def test_disabled_run_returns_immediately(self):
        signals = []
        w = PolygonWhaleWatcher({}, on_signal=signals.append)
        asyncio.run(w.run())
        assert signals == []


class TestFilterLogic:
    def _watcher(self, whales=None):
        signals = []
        w = PolygonWhaleWatcher(
            {"whale": {"enabled": True, "whales": whales or ["0xABC"]}},
            on_signal=signals.append,
        )
        return w, signals

    def test_ignores_non_polymarket_tx(self):
        w, signals = self._watcher()
        # Build a message to the wrong contract
        import json
        msg = json.dumps({
            "params": {"result": {
                "to": "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                "from": "0xabc",
            }}
        })
        w._handle_message(msg)
        assert signals == []

    def test_ignores_non_whale_from(self):
        w, signals = self._watcher(whales=["0xabc"])
        import json
        msg = json.dumps({
            "params": {"result": {
                "to": POLYMARKET_CLOB_EXCHANGE,
                "from": "0x0000000000000000000000000000000000000000",
            }}
        })
        w._handle_message(msg)
        assert signals == []

    def test_calldata_decoder_is_safe_no_op(self):
        # Until the real ABI is wired up, the decoder must return None
        # so no signals leak out even on matching tx.
        w, signals = self._watcher(whales=["0xabc"])
        import json
        msg = json.dumps({
            "params": {"result": {
                "to": POLYMARKET_CLOB_EXCHANGE,
                "from": "0xabc",
                "input": "0xdeadbeef",
            }}
        })
        w._handle_message(msg)
        assert signals == []


class TestMalformedMessage:
    def test_invalid_json_ignored(self):
        signals = []
        w = PolygonWhaleWatcher(
            {"whale": {"enabled": True, "whales": ["0xabc"]}},
            on_signal=signals.append,
        )
        w._handle_message("not valid json{{{")
        assert signals == []

    def test_missing_params_ignored(self):
        signals = []
        w = PolygonWhaleWatcher(
            {"whale": {"enabled": True, "whales": ["0xabc"]}},
            on_signal=signals.append,
        )
        w._handle_message('{"id": 1}')
        assert signals == []


class TestStopSignal:
    def test_stop_idempotent(self):
        w = PolygonWhaleWatcher({}, on_signal=lambda _: None)
        w.stop()
        w.stop()  # second call must not raise
        assert w._stop.is_set()
