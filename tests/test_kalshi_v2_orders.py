"""V2 order body construction, price-grid snapping and fixed-point encoding.
All offline."""
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from connectors.kalshi import KalshiConnector, snap_price, _fp


@pytest.fixture
def conn():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return KalshiConnector({"access_key": "k", "private_key": pem})


TAPERED = [
    {"start": "0.00", "end": "0.10", "step": "0.001"},
    {"start": "0.10", "end": "0.90", "step": "0.01"},
    {"start": "0.90", "end": "1.00", "step": "0.001"},
]


def test_fixed_point_strings():
    assert _fp(10, 2) == "10.00"
    assert _fp(0.5, 4) == "0.5000"
    assert _fp(0.12345, 4) == "0.1235"


def test_snap_buy_rounds_down_sell_rounds_up():
    assert snap_price(0.5449, None, "buy") == pytest.approx(0.54)
    assert snap_price(0.5449, None, "sell") == pytest.approx(0.55)


def test_snap_uses_market_grid():
    assert snap_price(0.0555, TAPERED, "buy") == pytest.approx(0.055)
    assert snap_price(0.9555, TAPERED, "sell") == pytest.approx(0.956)
    assert snap_price(0.555, TAPERED, "buy") == pytest.approx(0.55)


def test_buy_yes_is_bid_at_yes_price(conn):
    b = conn.build_order_body("MKT", "yes", 0.56, 10)
    assert b["side"] == "bid" and b["price"] == "0.5600" and b["count"] == "10.00"
    assert b["time_in_force"] == "immediate_or_cancel"
    assert b["self_trade_prevention_type"] == "taker_at_cross"
    assert "yes_price" not in b and "action" not in b   # legacy fields gone


def test_buy_no_is_ask_at_one_minus_no_price(conn):
    b = conn.build_order_body("MKT", "no", 0.30, 5)
    assert b["side"] == "ask" and b["price"] == "0.7000"


def test_sell_yes_is_ask_and_reduce_only(conn):
    b = conn.build_order_body("MKT", "yes", 0.61, 5, action="sell", reduce_only=True)
    assert b["side"] == "ask" and b["reduce_only"] is True


def test_sell_no_is_bid(conn):
    b = conn.build_order_body("MKT", "no", 0.25, 5, action="sell")
    assert b["side"] == "bid" and b["price"] == "0.7500"


def test_expiration_only_with_gtc(conn):
    b = conn.build_order_body("MKT", "yes", 0.5, 1, time_in_force="good_till_canceled",
                              expiration_time=1_800_000_000)
    assert b["expiration_time"] == 1_800_000_000
    b2 = conn.build_order_body("MKT", "yes", 0.5, 1, expiration_time=1_800_000_000)
    assert "expiration_time" not in b2


def test_fractional_count_and_grid_snap(conn):
    b = conn.build_order_body("MKT", "yes", 0.0555, 1.5, price_ranges=TAPERED)
    assert b["count"] == "1.50" and b["price"] == "0.0550"


def test_quote_normalization():
    q = KalshiConnector.quote({"yes_bid_dollars": "0.40", "yes_ask_dollars": "0.44",
                               "yes_ask_size_fp": "12.50"})
    assert q["mid"] == pytest.approx(0.42) and q["spread"] == pytest.approx(0.04)
    assert q["ask_size"] == 12.5
    q2 = KalshiConnector.quote({"last_price_dollars": "0.33"})
    assert q2["mid"] == pytest.approx(0.33) and q2["spread"] is None
