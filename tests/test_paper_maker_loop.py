"""End-to-end paper flow: opportunity -> resting order at the bid -> poll ->
fill only when traded through -> position booked at OUR price with 0 fee;
or expiry -> reservation released, nothing booked."""
import os
import tempfile
import unittest
from types import SimpleNamespace

import main as botmod
from env_config import BotConfig
from execution.paper import PaperTrader
from execution.resting import RestingOrders
from risk_manager import RiskManager


class _K:
    def __init__(self):
        self.m = {"ticker": "MKT-1", "yes_bid_dollars": "0.58", "yes_ask_dollars": "0.62", "status": "open"}
    def get_market(self, mid):
        return dict(self.m)
    @staticmethod
    def quote(m):
        yb, ya = float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])
        return {"yes_bid": yb, "yes_ask": ya, "mid": (yb + ya) / 2, "spread": round(ya - yb, 4),
                "ask_size": 100, "bid_size": 100, "price_ranges": None}


def _bot(tmp):
    b = botmod.PredMarketBot.__new__(botmod.PredMarketBot)
    b.config = BotConfig()
    b.config.mode = "paper"
    b.risk = RiskManager(b.config, state_file=os.path.join(tmp, "risk.json"))
    b.paper = PaperTrader({"trade_log": os.path.join(tmp, "t.json"),
                           "performance_log": os.path.join(tmp, "p.json"), "label": "PAPER"})
    b.resting = RestingOrders(os.path.join(tmp, "r.json"))
    b.kalshi = _K()
    b.telegram = SimpleNamespace(is_configured=lambda: False)
    b._live_ready = False
    return b


def _opp():
    return {"platform": "kalshi", "question": "q", "market_id": "MKT-1", "event_ticker": "EV-1",
            "category": "Politics", "signal": "YES", "ev": 0.1, "edge": 0.08, "model_prob": 0.68,
            "model_cell": "Politics|24", "market_price": 0.60, "yes_bid": 0.58, "yes_ask": 0.62,
            "spread": 0.04, "size_usd": 10.0, "shares": 16}


class TestPaperMakerLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = _bot(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rest_then_fill_when_traded_through(self):
        b, rc = self.b, self.b.config.risk
        self.assertTrue(b._paper_rest(_opp(), 10.0, "YES", rc))
        self.assertEqual(len(b.resting.active("paper")), 1)
        self.assertEqual(b.paper.get_open_positions(), [])
        self.assertFalse(b._paper_rest(_opp(), 10.0, "YES", rc))          # no duplicate
        b._poll_resting()                                                  # market unchanged: still resting
        self.assertEqual(len(b.resting.active("paper")), 1)
        b.kalshi.m.update({"yes_bid_dollars": "0.55", "yes_ask_dollars": "0.58"})   # trades through
        b._poll_resting()
        self.assertEqual(b.resting.active("paper"), [])
        pos = b.paper.get_open_positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["entry_price"], 0.58)                     # our price, not the mid/ask
        self.assertEqual(pos[0]["shares"], 17)
        self.assertEqual(pos[0]["fees_usd"], 0.0)                         # maker: no fee
        self.assertEqual(pos[0]["model_cell"], "Politics|24")

    def test_expiry_releases_reservation(self):
        b, rc = self.b, self.b.config.risk
        b._paper_rest(_opp(), 10.0, "YES", rc)
        row = b.resting.active("paper")[0]
        row["expires_ts"] = 0                                               # force expiry
        b._poll_resting()
        self.assertEqual(b.resting.active("paper"), [])
        self.assertEqual(b.paper.get_open_positions(), [])
        self.assertTrue(b.risk.can_trade("MKT-1", 5.0, "yes")[0])
        self.assertEqual(b.risk.get_status()["trade_count"], 0)

    def test_edge_rechecked_at_resting_price(self):
        b, rc = self.b, self.b.config.risk
        o = _opp()
        o["model_prob"] = 0.61                                              # 3pp at the bid < 4pp gate
        self.assertFalse(b._paper_rest(o, 10.0, "YES", rc))
        self.assertEqual(b.resting.active("paper"), [])


if __name__ == "__main__":
    unittest.main()


class TestCrashSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = _bot(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fill_is_idempotent_and_orphans_are_reconciled(self):
        b, rc = self.b, self.b.config.risk
        b._paper_rest(_opp(), 10.0, "YES", rc)
        row = b.resting.active("paper")[0]
        # Simulate the old failure mode: ledger says filled, book never got it.
        b.resting.settle(row, "filled", filled=17, avg_fill=0.58)
        self.assertEqual(b.paper.get_open_positions(), [])
        b._reconcile_resting()
        self.assertEqual(len(b.paper.get_open_positions()), 1)
        b._reconcile_resting()                                             # second pass: no duplicate
        b._book_rest_fill(row, b.paper, "again")                           # explicit re-book: no duplicate
        self.assertEqual(len(b.paper.get_open_positions()), 1)

    def test_unreadable_market_still_expires_and_releases(self):
        b, rc = self.b, self.b.config.risk
        b._paper_rest(_opp(), 10.0, "YES", rc)
        b.kalshi.get_market = lambda mid: None
        b._poll_resting()
        self.assertEqual(len(b.resting.active("paper")), 1)               # not expired yet: keep waiting
        b.resting.active("paper")[0]["expires_ts"] = 0
        b._poll_resting()
        self.assertEqual(b.resting.active("paper"), [])
        self.assertTrue(b.risk.can_trade("MKT-1", 5.0, "yes")[0])

    def test_settled_market_while_resting_never_fills(self):
        b, rc = self.b, self.b.config.risk
        b._paper_rest(_opp(), 10.0, "YES", rc)
        b.kalshi.m.update({"status": "settled", "yes_bid_dollars": "0.00", "yes_ask_dollars": "0.01"})
        b._poll_resting()
        self.assertEqual(b.paper.get_open_positions(), [])
        self.assertEqual(b.resting.active("paper"), [])


class TestModelStateAnnounce(unittest.TestCase):
    def test_announces_only_on_change(self):
        import os as _os
        from research.calibration import CalibratedModel
        tmp = tempfile.TemporaryDirectory()
        cwd = _os.getcwd()
        _os.chdir(tmp.name)
        try:
            b = _bot(tmp.name)
            sent = []
            b.telegram = SimpleNamespace(is_configured=lambda: True, send=lambda t, **k: sent.append(t))
            b.model = CalibratedModel({"generated": "g1", "n_rows": 10, "cells": {}})
            b._announce_model_state()
            self.assertEqual(len(sent), 1)
            self.assertIn("no usable cells", sent[0])
            b._announce_model_state()                       # same model: quiet
            self.assertEqual(len(sent), 1)
            b.model = CalibratedModel({"generated": "g2", "n_rows": 10, "cells": {
                "Politics|24": {"used": True, "b": 1.5, "n": 300, "brier_market_holdout": 0.2, "brier_calibrated_holdout": 0.19},
                "ALL|24": {"used": True, "b": 1.2, "n": 900, "brier_market_holdout": 0.2, "brier_calibrated_holdout": 0.19}}})
            b._announce_model_state()
            self.assertEqual(len(sent), 2)
            self.assertIn("ARMED", sent[1])
            self.assertIn("Politics|24", sent[1])
            self.assertNotIn("ALL|24", sent[1])
        finally:
            _os.chdir(cwd)
            tmp.cleanup()
