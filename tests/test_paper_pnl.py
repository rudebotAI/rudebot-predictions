"""Position-book P&L: same-leg convention, NO side, partial closes, Sharpe."""
import pytest

from execution.paper import PaperTrader


@pytest.fixture
def book(tmp_path):
    return PaperTrader({"trade_log": str(tmp_path / "t.json"),
                        "performance_log": str(tmp_path / "p.json")})


def _open(book, signal, price, usd):
    return book.open_position({"market_id": f"M-{signal}", "signal": signal,
                               "market_price": price, "question": "q"}, usd)


def test_yes_win_on_resolution(book):
    t = _open(book, "YES", 0.40, 10.0)          # 25 shares
    c = book.close_position(t["id"], 1.0, "resolved")
    assert c["pnl"] == pytest.approx(15.0)


def test_no_win_on_resolution_is_positive(book):
    t = _open(book, "NO", 0.70, 7.0)            # 10 shares of NO @ 0.70
    c = book.close_position(t["id"], 1.0, "resolved")   # NO resolves -> NO leg pays 1.0
    assert c["pnl"] == pytest.approx(3.0)


def test_no_loss_on_resolution_is_negative(book):
    t = _open(book, "NO", 0.70, 7.0)
    c = book.close_position(t["id"], 0.0, "resolved")
    assert c["pnl"] == pytest.approx(-7.0)


def test_no_mark_to_market_uses_no_leg(book):
    t = _open(book, "NO", 0.70, 7.0)
    # yes mid moves 0.30 -> 0.35 => NO leg 0.65 => small loss, not +50%
    c = book.close_position(t["id"], 0.65, "stop_loss")
    assert c["pnl"] == pytest.approx(-0.5)
    assert c["pnl_pct"] == pytest.approx(-7.14, abs=0.01)


def test_partial_close_keeps_remainder(book):
    t = _open(book, "YES", 0.50, 10.0)          # 20 shares
    c = book.close_position(t["id"], 0.60, "take_profit", shares=5)
    assert c["partial"] and c["pnl"] == pytest.approx(0.5)
    remaining = book.get_open_positions()[0]
    assert remaining["shares"] == 15 and remaining["size_usd"] == pytest.approx(7.5)


def test_fill_override_books_actual_fill(book):
    t = book.open_position({"market_id": "M", "signal": "YES", "market_price": 0.50},
                           10.0, fill={"price": 0.52, "contracts": 7, "size_usd": 3.64,
                                       "order_id": "o1"})
    assert t["entry_price"] == 0.52 and t["shares"] == 7 and t["order_id"] == "o1"


def test_performance_sharpe(book):
    for i, (p, x) in enumerate([(0.5, 0.6), (0.5, 0.55), (0.5, 0.45), (0.5, 0.62)]):
        t = book.open_position({"market_id": f"M{i}", "signal": "YES", "market_price": p}, 10.0)
        book.close_position(t["id"], x, "resolved")
    perf = book.get_performance()
    assert perf["total_trades"] == 4 and perf["wins"] == 3
    assert perf["sharpe"] is not None and perf["sharpe"] > 0


def test_fees_net_out_of_pnl(book):
    t = book.open_position({"market_id": "M", "signal": "YES", "market_price": 0.50}, 10.0,
                           fill={"price": 0.50, "contracts": 20, "size_usd": 10.0,
                                 "fee_per_contract": 0.0175})
    assert t["fees_usd"] == pytest.approx(0.35)
    c = book.close_position(t["id"], 0.60, "take_profit", fees_usd=0.30)
    assert c["pnl"] == pytest.approx(2.0 - 0.35 - 0.30)
