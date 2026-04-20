"""Tests for engines/market_maker.py — two-sided GTD quoting."""

from engines.market_maker import DEFAULTS, MarketMaker


def _book(bid=0.49, ask=0.51):
    return {"bids": [[bid, 500]], "asks": [[ask, 500]]}


def _mm(**overrides):
    cfg = {
        "mode": "paper",
        "market_maker": {
            "enabled": True,
            "markets": ["m1"],
            "requote_cooldown_sec": 0,
            **overrides,
        },
    }
    return MarketMaker(cfg, deps={"book": lambda mid: _book()})


class TestDisabledByDefault:
    def test_default_disabled(self):
        assert DEFAULTS["enabled"] is False

    def test_disabled_returns_no_intents(self):
        mm = MarketMaker({}, deps={"book": lambda _: _book()})
        assert mm.tick() == []

    def test_empty_market_list_no_quotes(self):
        mm = MarketMaker(
            {"market_maker": {"enabled": True, "markets": []}},
            deps={"book": lambda _: _book()},
        )
        assert mm.tick() == []


class TestBasicQuoting:
    def test_emits_bid_and_ask(self):
        mm = _mm(base_spread=0.04, quote_size_usd=2.0)
        intents = mm.tick()
        assert len(intents) == 2
        kinds = sorted(i["kind"] for i in intents)
        assert kinds == ["quote_ask", "quote_bid"]
        for i in intents:
            assert i["size_usd"] == 2.0
            assert i["tif"] == "GTD"

    def test_quotes_straddle_mid(self):
        mm = _mm(base_spread=0.04)
        intents = mm.tick()
        bid = next(i for i in intents if i["kind"] == "quote_bid")
        ask = next(i for i in intents if i["kind"] == "quote_ask")
        assert bid["price"] < ask["price"]


class TestInventorySkew:
    def test_heavy_long_yes_skews_quotes_down(self):
        mm = _mm(base_spread=0.04, skew_strength=0.5, max_inventory_abs=20.0)
        # Simulate a buy fill to build YES inventory
        mm.on_fill({"market_id": "m1", "side": "YES",
                    "direction": "BUY", "shares": 10})
        intents = mm.tick()
        bid = next(i for i in intents if i["kind"] == "quote_bid")
        # With positive inventory, bid should move BELOW mid - spread/2
        baseline_bid = 0.50 - 0.04 / 2
        assert bid["price"] < baseline_bid

    def test_inventory_cap_triggers_flatten_mode(self):
        mm = _mm(base_spread=0.04, max_inventory_abs=5.0)
        mm.on_fill({"market_id": "m1", "side": "YES",
                    "direction": "BUY", "shares": 10})  # way over cap
        intents = mm.tick()
        # At cap, only one side should be quoted (the flatten side)
        kinds = [i["kind"] for i in intents]
        assert len(kinds) == 1


class TestCooldown:
    def test_cooldown_gates_requote(self):
        mm = MarketMaker(
            {"market_maker": {"enabled": True, "markets": ["m1"],
                              "requote_cooldown_sec": 3600}},
            deps={"book": lambda _: _book()},
        )
        first = mm.tick()
        assert len(first) == 2
        second = mm.tick()
        # Within cooldown → no new quotes
        assert second == []


class TestNoBookNoOp:
    def test_missing_book_callback_returns_empty(self):
        mm = MarketMaker(
            {"market_maker": {"enabled": True, "markets": ["m1"]}},
            deps={},  # no book fn
        )
        assert mm.tick() == []

    def test_book_raises_returns_empty(self):
        def bad_book(_):
            raise RuntimeError("API down")
        mm = MarketMaker(
            {"market_maker": {"enabled": True, "markets": ["m1"]}},
            deps={"book": bad_book},
        )
        assert mm.tick() == []
