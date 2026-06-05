"""
Tests for the resolution-horizon gate, fee-adjusted EV, and annualized
ranking in engines/scanner.py.

Motivation: live scans surfaced markets resolving in 2045+ (Next Romania
PM, New Pope) -- positive raw EV but dead capital. Policy: skip anything
resolving >90 days out, charge the Kalshi fee (0.07*P*(1-P)) inside EV,
and rank by EV per unit time.
"""

import unittest
from datetime import datetime, timedelta, timezone

from engines.scanner import EVScanner, parse_days_to_resolution, KALSHI_FEE_RATE


def iso_in(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def market(days: float, yes=0.10, no=0.85, vol=20000, **kw):
    # yes=0.10 triggers the longshot-bias correction (model_prob ~0.075),
    # making the NO side +EV: model_no ~0.925 vs no_price 0.85.
    m = {
        "platform": "kalshi", "question": f"resolves in {days}d",
        "market_id": f"TKR-{days}", "yes_price": yes, "no_price": no,
        "volume": vol, "volume_24h": vol, "end_date": iso_in(days),
    }
    m.update(kw)
    return m


def make_scanner(**overrides):
    cfg = {"min_ev_threshold": 0.01, "min_market_volume": 100,
           "max_days_to_resolution": 90}
    cfg.update(overrides)
    return EVScanner(cfg)


class TestParseDays(unittest.TestCase):
    def test_parses_zulu_iso(self):
        self.assertAlmostEqual(parse_days_to_resolution(iso_in(30)), 30, delta=0.1)

    def test_handles_garbage(self):
        self.assertIsNone(parse_days_to_resolution(""))
        self.assertIsNone(parse_days_to_resolution(None))
        self.assertIsNone(parse_days_to_resolution("not-a-date"))


class TestHorizonGate(unittest.TestCase):
    def test_skips_markets_beyond_90_days(self):
        scanner = make_scanner()
        opps = scanner.scan([market(30), market(91), market(7000)])  # 7000d ~ 2045
        kept_days = {o["days_to_resolution"] for o in opps}
        self.assertTrue(all(d <= 90 for d in kept_days))
        self.assertFalse(any(d > 90 for d in kept_days))

    def test_skips_already_closed_and_undated(self):
        scanner = make_scanner()
        undated = market(30); undated["end_date"] = ""
        closed = market(-1)
        self.assertEqual(scanner.scan([undated, closed]), [])

    def test_horizon_is_configurable(self):
        scanner = make_scanner(max_days_to_resolution=10)
        opps = scanner.scan([market(5), market(30)])
        self.assertTrue(all(o["days_to_resolution"] <= 10 for o in opps))


class TestFeeAdjustedEV(unittest.TestCase):
    def test_fee_reduces_ev(self):
        scanner = make_scanner()
        price, prob = 0.30, 0.40
        fee = KALSHI_FEE_RATE * price * (1 - price)
        expected = round((prob - price - fee) / price, 4)
        self.assertEqual(scanner.compute_ev(prob, price), expected)

    def test_sub_fee_edge_is_no_opportunity(self):
        scanner = make_scanner()
        # Edge of 0.01 at p=0.5: fee = 0.0175 > edge -> net negative -> 0
        self.assertEqual(scanner.compute_ev(0.51, 0.50), 0.0)

    def test_fee_rate_configurable(self):
        scanner = make_scanner(fee_rate=0.0)
        self.assertGreater(scanner.compute_ev(0.51, 0.50), 0.0)


class TestAnnualizedRanking(unittest.TestCase):
    def test_near_term_ranks_above_far_term_at_equal_ev(self):
        scanner = make_scanner()
        near, far = market(7), market(85)
        opps = scanner.scan([far, near])
        self.assertGreaterEqual(len(opps), 2)
        self.assertLess(opps[0]["days_to_resolution"], opps[1]["days_to_resolution"])
        self.assertGreaterEqual(opps[0]["ev_annualized"], opps[1]["ev_annualized"])

    def test_opportunity_carries_time_fields(self):
        scanner = make_scanner()
        opps = scanner.scan([market(30)])
        self.assertTrue(opps)
        self.assertIn("days_to_resolution", opps[0])
        self.assertIn("ev_annualized", opps[0])


if __name__ == "__main__":
    unittest.main()
