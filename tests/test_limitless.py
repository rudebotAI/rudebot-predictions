"""Tests for connectors/limitless.py — read-only venue adapter."""
import pytest

from connectors.limitless import DEFAULTS, LimitlessConnector


class TestDisabledByDefault:
    def test_default_disabled(self):
        assert DEFAULTS["enabled"] is False

    def test_list_markets_disabled_returns_empty(self):
        c = LimitlessConnector({})
        assert c.list_markets() == []

    def test_get_orderbook_disabled_returns_none(self):
        c = LimitlessConnector({})
        assert c.get_orderbook("m1") is None


class TestWriteSideRefuses:
    def test_place_order_raises_not_implemented(self):
        c = LimitlessConnector({"limitless": {"enabled": True}})
        with pytest.raises(NotImplementedError, match="intentionally not implemented"):
            c.place_order("m1", "YES", 0.5, 1.0)


class TestVenueTag:
    def test_venue_constant_set(self):
        assert LimitlessConnector.VENUE == "limitless"
