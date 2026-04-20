"""Tests for execution/orders.py — FAK/GTD primitives + depth guard.

Focused on the guard rails, not the happy path.
"""
import time

import pytest

from execution.orders import (
    DEFAULTS,
    DepthGuardError,
    OrderIntent,
    OrderTIF,
    Side,
    check_depth,
    make_client_order_id,
)


def _buy(price=0.50, size_usd=5.0):
    return OrderIntent(
        market_id="m1",
        outcome="YES",
        side=Side.BUY,
        price=price,
        size_usd=size_usd,
        source="test",
    )


def _sell(price=0.50, size_usd=5.0):
    return OrderIntent(
        market_id="m1",
        outcome="YES",
        side=Side.SELL,
        price=price,
        size_usd=size_usd,
        source="test",
    )


class TestOrderIntent:
    def test_gtd_sets_default_expiry(self):
        now = time.time()
        intent = OrderIntent(
            market_id="m1", outcome="YES", side=Side.BUY,
            price=0.5, size_usd=1.0, tif=OrderTIF.GTD,
        )
        assert intent.gtd_expiry_epoch is not None
        assert intent.gtd_expiry_epoch >= now + 30  # ~60s default, allow slack

    def test_fak_does_not_set_expiry(self):
        intent = OrderIntent(
            market_id="m1", outcome="YES", side=Side.BUY,
            price=0.5, size_usd=1.0, tif=OrderTIF.FAK,
        )
        assert intent.gtd_expiry_epoch is None

    def test_explicit_expiry_preserved(self):
        want = time.time() + 300
        intent = OrderIntent(
            market_id="m1", outcome="YES", side=Side.BUY,
            price=0.5, size_usd=1.0, tif=OrderTIF.GTD,
            gtd_expiry_epoch=want,
        )
        assert intent.gtd_expiry_epoch == want


class TestDepthGuardEmptyBook:
    def test_empty_book_raises(self):
        with pytest.raises(DepthGuardError, match="no BUY side depth"):
            check_depth(_buy(), {"bids": [], "asks": []})

    def test_missing_asks_raises_for_buy(self):
        with pytest.raises(DepthGuardError):
            check_depth(_buy(), {"bids": [[0.5, 1000]]})

    def test_missing_bids_raises_for_sell(self):
        with pytest.raises(DepthGuardError):
            check_depth(_sell(), {"asks": [[0.5, 1000]]})


class TestDepthGuardInsufficientDepth:
    def test_thin_book_rejected(self):
        # Only $10 at the price → below $200 default
        with pytest.raises(DepthGuardError, match="within tolerance"):
            check_depth(_buy(price=0.50, size_usd=1.0),
                       {"bids": [], "asks": [[0.50, 20]]})

    def test_depth_outside_slippage_tolerance_ignored(self):
        # Huge depth but all 5¢ away — beyond default 1¢ tolerance
        with pytest.raises(DepthGuardError):
            check_depth(_buy(price=0.50, size_usd=10.0),
                       {"asks": [[0.55, 10000], [0.56, 10000]]})

    def test_depth_within_tolerance_accepted(self):
        # $500 of depth within tolerance → fine
        check_depth(_buy(price=0.50, size_usd=5.0),
                   {"asks": [[0.50, 1000]]})  # 0.50 * 1000 = $500

    def test_custom_min_depth_enforced(self):
        # Raising min to $1000 flips prior-passing case into failure
        with pytest.raises(DepthGuardError):
            check_depth(
                _buy(price=0.50, size_usd=5.0),
                {"asks": [[0.50, 1000]]},
                cfg={"min_depth_usd": 1000.01},
            )


class TestDepthGuardOrderSizeExceedsDepth:
    def test_order_larger_than_available_depth_rejected(self):
        # $500 available, ordering $1000 → reject
        with pytest.raises(DepthGuardError, match="exceeds available"):
            check_depth(_buy(price=0.50, size_usd=1000.0),
                       {"asks": [[0.50, 1000]]})


class TestDepthGuardSellSide:
    def test_sell_reads_bids(self):
        # Sell should read bids, ignore asks
        check_depth(_sell(price=0.50, size_usd=5.0),
                   {"bids": [[0.50, 1000]], "asks": []})

    def test_sell_rejects_when_bids_below_tolerance(self):
        # Bids 5¢ below target → outside tolerance
        with pytest.raises(DepthGuardError):
            check_depth(_sell(price=0.50, size_usd=5.0),
                       {"bids": [[0.45, 10000]], "asks": []})


class TestClientOrderId:
    def test_stable_within_bucket(self):
        a = make_client_order_id("whale", "m1", salt="tx123")
        b = make_client_order_id("whale", "m1", salt="tx123")
        assert a == b

    def test_different_salts_differ(self):
        a = make_client_order_id("whale", "m1", salt="tx1")
        b = make_client_order_id("whale", "m1", salt="tx2")
        assert a != b

    def test_contains_source_prefix(self):
        oid = make_client_order_id("copy_trader", "m1", salt="x")
        assert oid.startswith("rudebot-copy_t-")
