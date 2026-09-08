"""
Cell-aware (targeted) scan: horizon windows, frequency classes, discovery
budget fairness, and requote freshness -- all against a fake HTTP layer.
"""
import math
import time
from datetime import datetime, timedelta, timezone

import pytest

from connectors.kalshi import KalshiConnector
from research.calibration import freq_class, horizon_window_hours, cell_group


# ---- calibration helpers ---------------------------------------------------------------

def test_freq_class_splits_recurring_from_one_off():
    for f in ("daily", "hourly", "weekly", "fifteen_min", "custom", "Daily"):
        assert freq_class(f) == "recurring"
    for f in (None, "", "one_off", "single"):
        assert freq_class(f) == "one_off"
    assert cell_group("Sports", "daily") == "Sports/recurring"
    assert cell_group(None, None) == "?/one_off"


def test_horizon_windows_tile_the_axis_without_gaps():
    keys = ("1", "6", "24", "72", "168")
    prev_hi = None
    for k in keys:
        lo, hi = horizon_window_hours(k)
        assert lo < hi
        if prev_hi is not None:
            assert lo == pytest.approx(prev_hi)
        prev_hi = hi
    lo24, hi24 = horizon_window_hours(24)          # int accepted too
    assert lo24 == pytest.approx(math.sqrt(6 * 24))
    assert hi24 == pytest.approx(math.sqrt(24 * 72))
    assert horizon_window_hours("1")[0] == 0.25
    with pytest.raises(ValueError):
        horizon_window_hours("5")


# ---- fake connector -------------------------------------------------------------------------

def _iso(hours_from_now: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours_from_now)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _market(ticker, event, hours, yb="0.40", ya="0.42", vol24="100", status="open", exp_hours=None):
    return {"ticker": ticker, "event_ticker": event, "status": status, "title": ticker,
            "yes_bid_dollars": yb, "yes_ask_dollars": ya, "volume_fp": "500", "open_interest_fp": "50",
            "volume_24h_fp": vol24, "close_time": _iso(hours),
            "expected_expiration_time": _iso(exp_hours if exp_hours is not None else hours)}


class FakeKalshi(KalshiConnector):
    def __init__(self, catalog, index, series_markets, event_markets=None):
        super().__init__({})
        self._catalog, self._catalog_ts = catalog, time.time()
        self._event_index, self._event_index_ts = index, time.time()
        self._series_markets = series_markets
        self._event_markets = event_markets or {}
        self.calls = []

    def _http_get(self, path, timeout=10):
        self.calls.append(path)
        if path.startswith("/markets?series_ticker="):
            st = path.split("series_ticker=")[1].split("&")[0]
            return {"markets": self._series_markets.get(st, [])}
        if path.startswith("/markets?event_ticker="):
            ev = path.split("event_ticker=")[1].split("&")[0]
            return {"markets": self._event_markets.get(ev, [])}
        if path.startswith("/series/"):
            return {"series": {"fee_type": "quadratic", "fee_multiplier": "1"}}
        return {}


def _targets():
    out = []
    for cat, fc, h in (("Sports", "recurring", 24), ("Science and Technology", "one_off", 72)):
        lo, hi = horizon_window_hours(h)
        out.append({"cell": f"{cat}/{fc}|{h}", "category": cat, "freq_class": fc, "horizon": h, "lo_h": lo, "hi_h": hi})
    return out


def _fixture():
    catalog = {"KXMLB": {"category": "Sports", "frequency": "daily"},
               "KXNBA": {"category": "Sports", "frequency": "daily"},
               "KXNFLSZN": {"category": "Sports", "frequency": None},         # one_off sports -> no cell
               "KXSPACEX": {"category": "Science and Technology", "frequency": None},
               "KXPRES": {"category": "Politics", "frequency": None}}
    index = {"KXMLB": {"category": "Sports", "events": 30}, "KXNBA": {"category": "Sports", "events": 20},
             "KXNFLSZN": {"category": "Sports", "events": 5},
             "KXSPACEX": {"category": "Science and Technology", "events": 2},
             "KXPRES": {"category": "Politics", "events": 40}}
    series_markets = {
        "KXMLB": [_market("MLB-A", "EV-MLB-1", 20), _market("MLB-B", "EV-MLB-1", 20, vol24="900"),
                  _market("MLB-OLD", "EV-MLB-2", 3),                        # out of window (too soon)
                  _market("MLB-STALE", "EV-MLB-3", 20, exp_hours=-1)],      # expected_expiration in the past
        "KXNBA": [_market("NBA-A", "EV-NBA-1", 30)],
        "KXSPACEX": [_market("SPX-A", "EV-SPX-1", 60), _market("SPX-FAR", "EV-SPX-2", 500)],
        "KXPRES": [_market("PRES-A", "EV-PRES", 24)],
    }
    return catalog, index, series_markets


def test_discover_targets_filters_by_cell_and_window():
    catalog, index, sm = _fixture()
    k = FakeKalshi(catalog, index, sm)
    rows = k.discover_targets(_targets())
    ids = {r["market_id"] for r in rows}
    assert ids == {"MLB-A", "MLB-B", "NBA-A", "SPX-A"}
    cells = {r["market_id"]: r["scan_cell"] for r in rows}
    assert cells["MLB-A"] == "Sports/recurring|24" and cells["SPX-A"] == "Science and Technology/one_off|72"
    assert rows[0]["market_id"] == "MLB-B"                      # sorted by 24h volume
    assert all(r["frequency"] == "daily" for r in rows if r["category"] == "Sports")
    assert not any("KXPRES" in c or "KXNFLSZN" in c for c in k.calls)   # never queried


def test_discover_budget_is_shared_across_cells():
    catalog, index, sm = _fixture()
    k = FakeKalshi(catalog, index, sm)
    rows = k.discover_targets(_targets(), max_series_calls=2)
    # round-robin: 1 Sports series (busiest, KXMLB) + 1 Science series, not 2 Sports
    assert {r["series_ticker"] for r in rows} == {"KXMLB", "KXSPACEX"}


def test_discover_per_series_cap():
    catalog, index, sm = _fixture()
    k = FakeKalshi(catalog, index, sm)
    rows = k.discover_targets(_targets(), per_series_cap=1)
    assert [r["market_id"] for r in rows if r["series_ticker"] == "KXMLB"] == ["MLB-B"]


def test_requote_refreshes_and_drops_closed():
    catalog, index, sm = _fixture()
    k = FakeKalshi(catalog, index, sm)
    cands = k.discover_targets(_targets())
    fresh = {"EV-MLB-1": [_market("MLB-A", "EV-MLB-1", 19, yb="0.50", ya="0.52"),
                          _market("MLB-B", "EV-MLB-1", 19, status="closed")],
             "EV-NBA-1": [_market("NBA-A", "EV-NBA-1", 29)],
             "EV-SPX-1": []}                                     # vanished
    k._event_markets = fresh
    k.calls.clear()
    out = k.requote_candidates(cands, max_events=10)
    by = {r["market_id"]: r for r in out}
    assert set(by) == {"MLB-A", "NBA-A"}
    assert by["MLB-A"]["yes_bid"] == pytest.approx(0.50)         # fresh quote, not the snapshot
    assert by["MLB-A"]["scan_cell"] == "Sports/recurring|24"     # cell tag survives the requote
    assert len(k.calls) == 3                                     # one call per event


def test_requote_max_events_prefers_busiest():
    catalog, index, sm = _fixture()
    k = FakeKalshi(catalog, index, sm)
    cands = k.discover_targets(_targets())
    k._event_markets = {"EV-MLB-1": sm["KXMLB"][:2]}
    k.calls.clear()
    out = k.requote_candidates(cands, max_events=1)
    assert len(k.calls) == 1 and "EV-MLB-1" in k.calls[0]
    assert {r["market_id"] for r in out} == {"MLB-A", "MLB-B"}
