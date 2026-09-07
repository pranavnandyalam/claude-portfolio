from portfolio.safety.broker import PositionSnap
from portfolio.safety.config import Limits
from portfolio.safety.rebalance import plan_orders

BIG = Limits(max_order_notional=1_000_000)


def pos(sym, qty, px, avail=None):
    return PositionSnap(sym, qty, qty if avail is None else avail, qty * px, px)


def test_within_drift_band_no_order():
    p = plan_orders({"AAPL": 0.10}, {"AAPL": pos("AAPL", 99, 100.0)}, {"AAPL": 100.0}, 100_000, 90_100, BIG)
    assert p.orders == ()


def test_exit_sells_everything_even_below_min_notional():
    p = plan_orders({}, {"AAPL": pos("AAPL", 3, 10.0)}, {"AAPL": 10.0}, 100_000, 100_000, BIG)
    assert len(p.orders) == 1 and p.orders[0].side == "sell" and p.orders[0].qty == 3 and p.orders[0].reason == "exit"


def test_whole_shares_and_position_clip():
    p = plan_orders({"AAPL": 0.30}, {}, {"AAPL": 333.0}, 100_000, 100_000, BIG)
    assert p.orders[0].qty == 30  # clipped to 10% -> 10_000 / 333
    assert any("clipped" in s for s in p.skipped)


def test_sell_limited_to_qty_available():
    p = plan_orders({}, {"AAPL": pos("AAPL", 100, 100.0, avail=40)}, {"AAPL": 100.0}, 100_000, 100_000, BIG)
    assert p.orders[0].qty == 40


def test_turnover_cap_prioritizes_sells_then_largest_buys():
    lim = Limits(max_order_notional=1_000_000, max_turnover_per_day=0.10)
    p = plan_orders({"AAPL": 0.10, "MSFT": 0.10}, {"GOOG": pos("GOOG", 100, 80.0)},
                    {"AAPL": 100.0, "MSFT": 200.0, "GOOG": 80.0}, 100_000, 92_000, lim)
    assert [o.side for o in p.orders][:1] == ["sell"]
    assert p.turnover <= 0.10 + 1e-9 and any("turnover" in s or "cap" in s for s in p.skipped)


def test_cash_budget_limits_buys():
    p = plan_orders({"AAPL": 0.10}, {}, {"AAPL": 100.0}, 100_000, 1_000, BIG)
    assert p.orders[0].qty == 10


def test_breaker_flags_drop_buys():
    p = plan_orders({"AAPL": 0.10}, {}, {"AAPL": 100.0}, 100_000, 100_000, BIG, allow_buys=False)
    assert p.orders == () and any("breaker" in s for s in p.skipped)


def test_max_order_notional_caps_each_order():
    p = plan_orders({"AAPL": 0.10}, {}, {"AAPL": 100.0}, 100_000, 100_000, Limits())
    assert p.orders[0].qty == 50 and "capped_max_order_notional" in p.orders[0].reason


def test_plan_hash_is_stable():
    a = plan_orders({"AAPL": 0.10}, {}, {"AAPL": 100.0}, 100_000, 100_000, BIG)
    b = plan_orders({"AAPL": 0.10}, {}, {"AAPL": 100.0}, 100_000, 100_000, BIG)
    assert a.hash == b.hash
