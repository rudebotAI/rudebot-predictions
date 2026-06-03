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
    assert c.place_order("MKT-X", "yes", 50, 1) is None  # refuses, no network


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
    path = "/trade-api/v2/portfolio/orders"
    sig_b64 = c._sign(ts, method, path)

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
    assert c.place_order("MKT", "maybe", 50, 1)["error"]      # bad side
    assert c.place_order("MKT", "yes", 0, 1)["error"]          # bad price
    assert c.place_order("MKT", "yes", 50, 0)["error"]         # bad count


# --- LiveTrader gating -----------------------------------------------------

class _FakeRisk:
    def __init__(self, allow=True):
        self.allow = allow
        self.entries = []

    def can_trade(self, market_id, position_usd, side="yes"):
        return (self.allow, "OK" if self.allow else "blocked")

    def record_entry(self, market_id, entry_price, shares, side, position_usd):
        self.entries.append((market_id, entry_price, shares, side, position_usd))


class _FakeKalshi:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def place_order(self, market_id, side, price_cents, count):
        self.calls.append((market_id, side, price_cents, count))
        return self.result


OPP = {"market_id": "MKT-X", "signal": "yes", "market_price": 0.50}


def test_live_trader_disabled_by_default():
    lt = LiveTrader(_FakeKalshi({"order_id": "x"}), _FakeRisk())
    out = lt.execute(OPP, 10.0)
    assert out.get("error") == "Live trading not enabled"


def test_live_trader_executes_when_enabled():
    risk = _FakeRisk(allow=True)
    kal = _FakeKalshi({"order_id": "abc123"})
    lt = LiveTrader(kal, risk)
    lt.enable()
    out = lt.execute(OPP, 10.0)
    assert out.get("success") is True
    assert kal.calls == [("MKT-X", "yes", 50, 20)]      # $10 / $0.50 = 20 contracts @ 50c
    assert len(risk.entries) == 1                        # entry recorded


def test_live_trader_respects_risk_block():
    kal = _FakeKalshi({"order_id": "x"})
    lt = LiveTrader(kal, _FakeRisk(allow=False))
    lt.enable()
    out = lt.execute(OPP, 10.0)
    assert "Risk blocked" in out.get("error", "")
    assert kal.calls == []                               # never reached the broker


def test_live_trader_rejects_zero_price():
    lt = LiveTrader(_FakeKalshi({"order_id": "x"}), _FakeRisk())
    lt.enable()
    out = lt.execute({"market_id": "MKT-X", "signal": "yes", "market_price": 0}, 10.0)
    assert out.get("error")


def test_live_trader_propagates_broker_failure():
    lt = LiveTrader(_FakeKalshi(None), _FakeRisk())  # connector returns None
    lt.enable()
    out = lt.execute(OPP, 10.0)
    assert out.get("error")


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
