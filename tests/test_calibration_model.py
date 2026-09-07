"""Predict stage: calibration fit recovers known slopes, refuses noise, and the
scanner only finds edge when a usable cell exists."""
import math
import random
import unittest
from datetime import datetime, timedelta, timezone

from engines.scanner import EVScanner
from research.calibration import (CalibratedModel, build_model, fit_cell, fit_logistic,
                                  logit, sigmoid)


def _cell(slope, n=2000, seed=0):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        p = rng.uniform(0.05, 0.95)
        q = sigmoid(slope * logit(p))
        out.append((i, p, 1 if rng.random() < q else 0))
    return out


class TestFit(unittest.TestCase):
    def test_recovers_underconfident_slope(self):
        c = fit_cell(_cell(1.5))
        self.assertAlmostEqual(c["b"], 1.5, delta=0.15)
        self.assertTrue(c["used"])
        self.assertLess(c["brier_calibrated_holdout"], c["brier_market_holdout"])

    def test_recovers_overconfident_slope(self):
        c = fit_cell(_cell(0.7, seed=3))
        self.assertAlmostEqual(c["b"], 0.7, delta=0.12)
        self.assertTrue(c["used"])

    def test_calibrated_market_is_not_used(self):
        c = fit_cell(_cell(1.0, seed=5))
        self.assertFalse(c["used"])
        self.assertTrue(0.85 < c["b"] < 1.15)

    def test_small_cell_is_not_used(self):
        c = fit_cell(_cell(1.5, n=80))
        self.assertFalse(c["used"])
        self.assertIn("n=80", c["why"])

    def test_fit_logistic_identity_on_perfect_data(self):
        a, b = fit_logistic([0.0, 1.0, -1.0], [0, 1, 0])
        self.assertTrue(math.isfinite(a) and math.isfinite(b))


class TestModel(unittest.TestCase):
    def setUp(self):
        rows = []
        for i, p, y in _cell(1.5):
            rows.append({"category": "Politics", "close_ts": i, "y": y, "p": {"24": p, "168": p}})
        for i, p, y in _cell(1.0, seed=9):
            rows.append({"category": "Sports", "close_ts": i, "y": y, "p": {"24": p}})
        self.model = CalibratedModel(build_model(rows, source="test"))

    def test_horizon_key_nearest_log(self):
        self.assertEqual(CalibratedModel.horizon_key(20), "24")
        self.assertEqual(CalibratedModel.horizon_key(100), "72")
        self.assertEqual(CalibratedModel.horizon_key(0.1), "1")

    def test_prob_and_fallbacks(self):
        # Politics cell used: 0.70 pushed away from 0.5
        self.assertGreater(self.model.prob(0.70, "Politics", 20), 0.75)
        # Sports is calibrated -> its own cell unused -> market price, never the pooled cell
        e = self.model.explain(0.70, "Sports", 20)
        self.assertIsNone(e["cell"])
        self.assertEqual(e["edge"], 0.0)
        # Unknown category at a horizon with no pooled cell -> price
        self.assertEqual(CalibratedModel({}).prob(0.7, "X", 20), 0.7)
        self.assertIn("no usable", CalibratedModel({}).explain(0.7, "X", 20)["reason"])


class TestScannerUsesModel(unittest.TestCase):
    def _market(self, price=0.70, category="Politics", days=1):
        end = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")
        return {"yes_price": price, "no_price": 1 - price, "volume": 10_000, "end_date": end,
                "category": category, "question": "q", "market_id": "M"}

    def test_no_model_no_heuristic_means_no_edge(self):
        sc = EVScanner({"min_ev_threshold": 0.01, "min_market_volume": 10})
        self.assertEqual(sc.scan([self._market()]), [])
        m = self._market()
        self.assertEqual(sc.estimate_true_prob(m), 0.70)
        self.assertIn("no usable", m["model_reason"])

    def test_model_creates_edge_only_for_usable_cell(self):
        rows = [{"category": "Politics", "close_ts": i, "y": y, "p": {"24": p}} for i, p, y in _cell(1.6)]
        model = CalibratedModel(build_model(rows, source="t"))
        sc = EVScanner({"min_ev_threshold": 0.01, "min_market_volume": 10}, model=model)
        opps = sc.scan([self._market(0.70, "Politics"), self._market(0.70, "Weather")])
        self.assertEqual(len(opps), 1)
        self.assertEqual(opps[0]["signal"], "YES")
        self.assertEqual(opps[0]["model_cell"], "Politics|24")
        self.assertGreater(opps[0]["edge"], 0.04)

    def test_heuristic_only_when_explicitly_allowed(self):
        sc = EVScanner({"min_ev_threshold": 0.01, "min_market_volume": 10, "allow_heuristic": True})
        m = self._market(0.95)
        self.assertNotEqual(sc.estimate_true_prob(m), 0.95)


if __name__ == "__main__":
    unittest.main()


class TestSeriesFees(unittest.TestCase):
    def test_maker_style_uses_market_maker_rate(self):
        from research.calibration import CalibratedModel, build_model
        rows = [{"category": "Politics", "close_ts": i, "y": y, "p": {"24": p}} for i, p, y in _cell(1.6)]
        model = CalibratedModel(build_model(rows, source="t"))
        end = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        m = {"yes_price": 0.70, "no_price": 0.30, "volume": 10_000, "end_date": end, "category": "Politics",
             "question": "q", "market_id": "M", "taker_fee_rate": 0.035, "maker_fee_rate": 0.00875}
        taker = EVScanner({"min_ev_threshold": 0.0, "min_market_volume": 10, "entry_style": "taker"}, model=model)
        maker = EVScanner({"min_ev_threshold": 0.0, "min_market_volume": 10, "entry_style": "maker"}, model=model)
        self.assertAlmostEqual(taker.fee_rate_for(m), 0.035)
        self.assertAlmostEqual(maker.fee_rate_for(m), 0.00875)
        self.assertGreater(maker.scan([dict(m)])[0]["ev"], taker.scan([dict(m)])[0]["ev"])
        # unknown series: maker 0, taker default 0.07
        self.assertEqual(maker.fee_rate_for({}), 0.0)
        self.assertEqual(taker.fee_rate_for({}), 0.07)
