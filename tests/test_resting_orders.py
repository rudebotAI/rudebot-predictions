"""Execute stage v6.1: post-only resting entries (paper fill model, expiry
releases risk, live polling books only exchange-reported fills)."""
import os
import tempfile
import time
import unittest
from types import SimpleNamespace

from env_config import BotConfig
from execution.live import LiveTrader
from execution.paper import PaperTrader
from execution.resting import RestingOrders, own_leg_bid_ask
from risk_manager import RiskManager


def _opp(side="YES", price=0.60):
    return {"platform": "kalshi", "question": "q", "market_id": "MKT-1", "event_ticker": "EV-1",
            "category": "Politics", "signal": side, "ev": 0.1, "edge": 0.08, "model_prob": 0.68,
            "model_cell": "Politics|24", "market_price": price, "yes_bid": 0.58, "yes_ask": 0.62,
            "spread": 0.04, "size_usd": 10.0}


class TestLegMath(unittest.TestCase):
    def test_own_leg_bid_ask(self):
        self.assertEqual(own_leg_bid_ask("YES", 0.58, 0.62), (0.58, 0.62))
        b, a = own_leg_bid_ask("NO", 0.58, 0.62)
        self.assertAlmostEqual(b, 0.38)
        self.assertAlmostEqual(a, 0.42)


class TestPaperFillModel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = RestingOrders(os.path.join(self.tmp.name, "r.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_fills_only_when_market_trades_through(self):
        row = self.r.place(_opp("YES"), 0.58, 17, "paper", 600)
        self.assertFalse(RestingOrders.paper_would_fill(row, 0.58, 0.62))   # sitting on the bid: no
        self.assertFalse(RestingOrders.paper_would_fill(row, 0.57, 0.59))   # ask still above us
        self.assertTrue(RestingOrders.paper_would_fill(row, 0.55, 0.58))    # ask at our price: yes
        self.assertTrue(RestingOrders.paper_would_fill(row, 0.50, 0.54))    # traded through
        no = self.r.place({**_opp("NO"), "market_id": "MKT-2"}, 0.38, 26, "paper", 600)
        self.assertFalse(RestingOrders.paper_would_fill(no, 0.58, 0.62))   # NO ask = 0.42 > 0.38
        self.assertTrue(RestingOrders.paper_would_fill(no, 0.62, 0.66))    # NO ask = 0.38

    def test_persistence_and_summary(self):
        row = self.r.place(_opp(), 0.58, 17, "paper", 600)
        self.r.settle(row, "filled", filled=17, avg_fill=0.58)
        r2 = RestingOrders(self.r.path)
        self.assertEqual(r2.orders[0]["status"], "filled")
        self.assertEqual(r2.summary(), {"resting": 0, "settled": 1, "filled": 1, "fill_rate": 1.0})
        self.assertEqual(r2.fill_for_book(r2.orders[0])["size_usd"], round(0.58 * 17, 4))


class TestRiskReservation(unittest.TestCase):
    def test_release_frees_exposure_without_counting_a_trade(self):
        tmp = tempfile.TemporaryDirectory()
        cfg = BotConfig()
        cfg.mode = "paper"
        risk = RiskManager(cfg, state_file=os.path.join(tmp.name, "s.json"))
        risk.record_entry("MKT-1", 0.58, 17, "yes", 9.86, event_ticker="EV-1")
        self.assertFalse(risk.can_trade("MKT-1", 5.0, "yes")[0])         # duplicate blocked
        risk.release_entry("MKT-1")
        self.assertTrue(risk.can_trade("MKT-1", 5.0, "yes")[0])
        self.assertEqual(risk.get_status()["trade_count"], 0)
        tmp.cleanup()


class _FakeKalshi:
    def __init__(self):
        self.orders = {}
        self.placed = []
        self.market = {"ticker": "MKT-1", "yes_bid_dollars": "0.58", "yes_ask_dollars": "0.62",
                       "status": "open", "price_ranges": None}

    def get_market(self, mid):
        return dict(self.market)

    @staticmethod
    def quote(m):
        yb, ya = float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])
        return {"yes_bid": yb, "yes_ask": ya, "mid": (yb + ya) / 2, "spread": round(ya - yb, 4),
                "ask_size": 100, "bid_size": 100, "price_ranges": None}

    def place_order(self, **kw):
        self.placed.append(kw)
        oid = f"o{len(self.placed)}"
        self.orders[oid] = {"order_id": oid, "status": "resting", "fill_count": 0.0, "remaining_count": kw["count"]}
        return {"order_id": oid, "client_order_id": kw.get("client_order_id"), "fill_count": 0.0,
                "remaining_count": float(kw["count"]), "average_fill_price": None, "average_fee_paid": 0.0,
                "price": kw["price"], "book_side": "bid"}

    def get_order(self, oid):
        return dict(self.orders[oid])

    def cancel_order(self, oid, market_ticker=None):
        self.orders[oid]["status"] = "canceled"
        return True


class TestLiveMaker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = BotConfig()
        cfg.mode = "live"
        self.risk = RiskManager(cfg, state_file=os.path.join(self.tmp.name, "s.json"))
        self.k = _FakeKalshi()
        self.live = LiveTrader(self.k, self.risk, {"max_spread": 0.05})
        self.live.enable()

    def tearDown(self):
        self.tmp.cleanup()

    def test_places_post_only_gtc_at_bid_with_expiry(self):
        res = self.live.execute_maker(_opp("YES"), 10.0, 1800, nonce="1")
        self.assertTrue(res["success"] and res["resting"])
        kw = self.k.placed[0]
        self.assertEqual(kw["time_in_force"], "good_till_canceled")
        self.assertTrue(kw["post_only"])
        self.assertEqual(kw["price"], 0.58)
        self.assertEqual(kw["count"], 17)
        self.assertGreater(kw["expiration_time"], int(time.time()) + 1700)
        self.assertFalse(self.risk.can_trade("MKT-1", 5.0, "yes")[0])   # reserved

    def test_no_side_rests_at_no_bid(self):
        res = self.live.execute_maker(_opp("NO"), 10.0, 600, nonce="2")
        self.assertAlmostEqual(res["price"], 0.38)
        self.assertEqual(res["contracts"], 26)

    def test_wide_spread_refused(self):
        self.k.market["yes_ask_dollars"] = "0.70"
        self.assertIn("Spread", self.live.execute_maker(_opp("YES"), 10.0, 600)["error"])

    def test_poll_reports_exchange_truth(self):
        res = self.live.execute_maker(_opp("YES"), 10.0, 600, nonce="3")
        self.k.orders[res["order_id"]].update({"status": "executed", "fill_count": 17.0, "remaining_count": 0.0})
        st = self.live.poll_order(res["order_id"])
        self.assertEqual((st["status"], st["fill_count"]), ("executed", 17.0))


if __name__ == "__main__":
    unittest.main()
