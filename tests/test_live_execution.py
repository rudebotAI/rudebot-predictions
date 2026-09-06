"""
Tests for the live-trading path: Kalshi RSA-PSS request signing, auth gating,
and LiveTrader execution/risk gating. All offline — no network calls.
"""
import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from connectors.kalshi import KalshiConnector
from execution.live import LiveTrader


@pytest.fixture
def rsa_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return key, pem


# --- Auth presence ---------------------------------------------------------

def test_no_auth_means_no_trading():
    c = KalshiConnector({})  # paper-mode: no creds
    assert c.has_trading_auth() is False
    assert c.place_order("MKT-X", "yes", 0.50, 1) is None  # refuses, no network


def test_bad_private_key_disables_trading():
    c = KalshiConnector({"access_key": "abc", "private_key": "-----BEGIN nonsense-----"})
    assert c.has_trading_auth() is False


def test_valid_key_enables_trading(rsa_keypair):
    _key, pem = rsa_keypair
    c = KalshiConnector({"access_key": "key-id-123", "private_key": pem})
    assert c.has_trading_auth() is True


# --- Signature correctness (RSA-PSS SHA256, matches Kalshi spec) -----------

def test_signature_verifies_with_public_key(rsa_keypair):
    key, pem = rsa_keypair
    c = KalshiConnector({"access_key": "key-id-123", "private_key": pem})

    ts = "1730000000000"
    method = "POST"
    path = "/trade-api/v2/portfolio/events/orders"
    # Query strings must be stripped before signing (Kalshi spec).
    sig_b64 = c._sign(ts, method, path + "?limit=5")

    message = f"{ts}{method}{path}".encode()
    # Must not raise -> signature is valid for the same PSS/SHA256 params.
    key.public_key().verify(
        base64.b64decode(sig_b64),
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_signed_headers_shape(rsa_keypair):
    _key, pem = rsa_keypair
    c = KalshiConnector({"access_key": "key-id-123", "private_key": pem})
    h = c._signed_headers("GET", "/portfolio/balance")
    assert h["KALSHI-ACCESS-KEY"] == "key-id-123"
    assert h["KALSHI-ACCESS-SIGNATURE"]
    assert h["KALSHI-ACCESS-TIMESTAMP"].isdigit()


def test_place_order_rejects_bad_inputs(rsa_keypair):
    _key, pem = rsa_keypair
    c = KalshiConnector({"access_key": "k", "private_key": pem})
    # These return before any network call.
    assert c.place_order("MKT", "maybe", 0.5, 1)["error"]     # bad side
    assert c.place_order("MKT", "yes", 0, 1)["error"]          # bad price
    assert c.place_order("MKT", "yes", 1.0, 1)["error"]        # bad price
    assert c.place_order("MKT", "yes", 0.5, 0)["error"]        # bad count
    assert c.place_order("MKT", "yes", 0.5, 1, time_in_force="gtt")["error"]


# --- LiveTrader gating -----------------------------------------------------

class _FakeRisk:
    def __init__(self, allow=True):
        self.allow = allow
        self.entries = []

    def can_trade(self, market_id, position_usd, side="yes", event_ticker=None):
        return (self.allow, "OK" if self.allow else "blocked")

    def record_entry(self, market_id, entry_price, shares, side, position_usd, event_ticker=None):
        self.entries.append((market_id, entry_price, shares, side, position_usd))


MARKET = {
    "ticker": "MKT-X",
    "yes_bid_dollars": "0.4800", "yes_ask_dollars": "0.5000",
    "yes_bid_size_fp": "500.00", "yes_ask_size_fp": "500.00",
    "price_ranges": [{"start": "0.00", "end": "1.00", "step": "0.01"}],
}


class _FakeKalshi:
    """Stands in for KalshiConnector: serves a quote and echoes a V2 fill."""

    def __init__(self, result, market=None):
        self.result = result
        self.market = market or MARKET
        self.calls = []

    quote = staticmethod(KalshiConnector.quote)

    def get_market(self, market_id):
        return self.market

    def place_order(self, **kw):
        self.calls.append(kw)
        return self.result


OPP = {"market_id": "MKT-X", "signal": "yes", "market_price": 0.50}
FULL_FILL = {"order_id": "abc123", "fill_count": 19.0, "remaining_count": 0.0,
             "average_fill_price": 0.50, "price": 0.51}


def test_live_trader_disabled_by_default():
    lt = LiveTrader(_FakeKalshi(FULL_FILL), _FakeRisk())
    out = lt.execute(OPP, 10.0)
    assert out.get("error") == "Live trading not enabled"


def test_live_trader_executes_ioc_at_touch_plus_slippage():
    risk = _FakeRisk(allow=True)
    kal = _FakeKalshi(FULL_FILL)
    lt = LiveTrader(kal, risk)
    lt.enable()
    out = lt.execute(OPP, 10.0)
    assert out.get("success") is True
    call = kal.calls[0]
    assert call["time_in_force"] == "immediate_or_cancel"
    assert call["side"] == "yes" and call["action"] == "buy"
    assert abs(call["price"] - 0.51) < 1e-9          # ask 0.50 + 0.01 slippage
    assert call["count"] == 19                       # int($10 / 0.51)
    assert call["client_order_id"].startswith("rb-")
    # Booked at the exchange's average fill, for the filled quantity only.
    assert risk.entries == [("MKT-X", 0.50, 19.0, "yes", 9.5)]
    assert out["contracts"] == 19.0 and out["size_usd"] == 9.5


def test_live_trader_zero_fill_records_nothing():
    risk = _FakeRisk()
    kal = _FakeKalshi({**FULL_FILL, "fill_count": 0.0, "remaining_count": 0.0})
    lt = LiveTrader(kal, risk)
    lt.enable()
    out = lt.execute(OPP, 10.0)
    assert "No fill" in out["error"]
    assert risk.entries == []


def test_live_trader_partial_fill_books_filled_only():
    risk = _FakeRisk()
    kal = _FakeKalshi({**FULL_FILL, "fill_count": 5.0, "remaining_count": 14.0, "average_fill_price": 0.505})
    lt = LiveTrader(kal, risk)
    lt.enable()
    out = lt.execute(OPP, 10.0)
    assert out["success"] and out["contracts"] == 5.0
    assert risk.entries[0][2] == 5.0


def test_live_trader_no_side_prices_from_yes_bid():
    """Buying NO lifts the NO ask = 1 - yes_bid; fill price converts back to NO leg."""
    risk = _FakeRisk()
    kal = _FakeKalshi({**FULL_FILL, "average_fill_price": 0.48})  # yes-leg avg
    lt = LiveTrader(kal, risk)
    lt.enable()
    out = lt.execute({"market_id": "MKT-X", "signal": "no", "market_price": 0.52}, 10.0)
    call = kal.calls[0]
    assert call["side"] == "no"
    assert abs(call["price"] - 0.53) < 1e-9          # NO ask 0.52 + slippage
    assert abs(out["price"] - 0.52) < 1e-9           # 1 - 0.48
    assert risk.entries[0][1] == pytest.approx(0.52)


def test_live_trader_spread_guard():
    wide = {**MARKET, "yes_bid_dollars": "0.30", "yes_ask_dollars": "0.60"}
    lt = LiveTrader(_FakeKalshi(FULL_FILL, market=wide), _FakeRisk())
    lt.enable()
    out = lt.execute(OPP, 10.0)
    assert "Spread" in out["error"]


def test_live_trader_depth_limits_size():
    thin = {**MARKET, "yes_ask_size_fp": "3.00"}
    kal = _FakeKalshi({**FULL_FILL, "fill_count": 3.0}, market=thin)
    lt = LiveTrader(kal, _FakeRisk())
    lt.enable()
    out = lt.execute(OPP, 10.0)
    assert kal.calls[0]["count"] == 3
    assert out["success"]


def test_live_trader_respects_risk_block():
    kal = _FakeKalshi(FULL_FILL)
    lt = LiveTrader(kal, _FakeRisk(allow=False))
    lt.enable()
    out = lt.execute(OPP, 10.0)
    assert "Risk blocked" in out.get("error", "")
    assert kal.calls == []                               # never reached the broker


def test_live_trader_rejects_missing_quote():
    lt = LiveTrader(_FakeKalshi(FULL_FILL, market={"ticker": "MKT-X"}), _FakeRisk())
    lt.enable()
    out = lt.execute(OPP, 10.0)
    assert out.get("error")


def test_live_trader_propagates_broker_failure():
    lt = LiveTrader(_FakeKalshi(None), _FakeRisk())  # connector returns None
    lt.enable()
    out = lt.execute(OPP, 10.0)
    assert out.get("error")


def test_live_trader_close_sells_reduce_only():
    kal = _FakeKalshi({**FULL_FILL, "fill_count": 19.0, "average_fill_price": 0.47})
    lt = LiveTrader(kal, _FakeRisk())
    lt.enable()
    out = lt.close({"market_id": "MKT-X", "signal": "YES", "shares": 19}, reason="stop_loss")
    call = kal.calls[0]
    assert call["action"] == "sell" and call["reduce_only"] is True
    assert abs(call["price"] - 0.47) < 1e-9          # bid 0.48 - slippage
    assert out["success"] and out["price"] == pytest.approx(0.47)


# --- Tolerant key loading (recovers from copy-paste that drops PEM armor) ---

def test_bare_pkcs1_body_without_armor_still_loads(rsa_keypair):
    """A key body pasted WITHOUT -----BEGIN/END----- lines must still load."""
    key, _pem = rsa_keypair
    pkcs1 = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,  # PKCS#1 "RSA PRIVATE KEY"
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    # Strip the armor lines, leaving only the base64 body (the paste mistake).
    body = "\n".join(
        ln for ln in pkcs1.splitlines() if ln and "-----" not in ln
    )
    assert "BEGIN" not in body
    c = KalshiConnector({"access_key": "key-id", "private_key": body})
    assert c.has_trading_auth() is True          # recovered the framing
    assert c._signed_headers("GET", "/portfolio/balance")["KALSHI-ACCESS-SIGNATURE"]
