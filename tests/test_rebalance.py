import pytest

from portfolio.broker import Account, Position
from portfolio.rebalance import RebalanceParams, build_order_plan, normalize_targets, plan_to_rows


def acct(equity=100_000.0, cash=None):
    cash = equity if cash is None else cash
    return Account(equity=equity, cash=cash, buying_power=cash, last_equity=equity, status="ACTIVE", is_paper=True)


def pos(sym, qty, price):
    return Position(symbol=sym, qty=qty, market_value=qty * price, avg_entry_price=price, current_price=price)


def test_normalize_targets():
    assert normalize_targets({"spy": 0.5, "AGG": -0.1, "X": 0.0, "Y": float("nan")}) == {"SPY": 0.5}
    n = normalize_targets({"A": 0.8, "B": 0.4})
    assert abs(sum(n.values()) - 1.0) < 1e-12 and abs(n["A"] - 2 / 3) < 1e-12
    assert normalize_targets({"A": 0.3, "B": 0.3}) == {"A": 0.3, "B": 0.3}  # never scaled up


def test_initial_deployment_buys_with_cash_buffer():
    plan = build_order_plan({"SPY": 0.6, "AGG": 0.4}, [], acct(), {"SPY": 500.0, "AGG": 100.0}, "20260908")
    assert plan.blocked_reason == "" and len(plan.trades) == 2 and not plan.sells
    by = {t.symbol: t for t in plan.trades}
    assert by["SPY"].notional == pytest.approx(0.6 * 99_000, abs=0.01)
    assert by["AGG"].notional == pytest.approx(0.4 * 99_000, abs=0.01)
    assert by["SPY"].reason == "enter" and by["SPY"].qty is None
    assert plan.turnover_pct == pytest.approx(0.99, abs=1e-6)
    assert plan_to_rows(plan)[0]["symbol"] == "AGG"


def test_drift_band_skips_small_changes_and_exits_always_run():
    positions = [pos("SPY", 118.8, 500.0), pos("AGG", 396.0, 100.0), pos("OLD", 10, 50.0)]
    a = acct(equity=100_000, cash=100_000 - 59_400 - 39_600 - 500)
    plan = build_order_plan({"SPY": 0.605, "AGG": 0.395}, positions, a, {"SPY": 500.0, "AGG": 100.0, "OLD": 50.0}, "20260908")
    assert [t.symbol for t in plan.sells] == ["OLD"]
    assert plan.sells[0].qty == 10 and plan.sells[0].reason == "exit"
    assert not plan.buys
    reasons = dict(plan.skipped)
    assert "within band" in reasons["SPY"] and "within band" in reasons["AGG"]


def test_reduce_uses_qty_and_never_oversells():
    positions = [pos("SPY", 200.0, 500.0)]  # $100k in SPY, equity 100k, cash 0
    plan = build_order_plan({"SPY": 0.5, "AGG": 0.5}, positions, acct(equity=100_000, cash=0), {"SPY": 500.0, "AGG": 100.0}, "20260908")
    sell = plan.sells[0]
    assert sell.symbol == "SPY" and sell.qty == pytest.approx((100_000 - 0.5 * 99_000) / 500.0, abs=1e-6)
    assert sell.qty < 200.0
    buy = plan.buys[0]
    # sell proceeds (haircut) cover the buy → no scaling needed
    assert buy.notional == pytest.approx(0.5 * 99_000, abs=0.01)
    assert buy.notional <= sell.notional * (1 - 0.005) + 0.01
    assert not plan.notes
    assert plan.trades[0].side == "sell" and plan.trades[-1].side == "buy"
    # with zero cash and a much larger buy than the sell frees up, buys are scaled
    plan2 = build_order_plan({"SPY": 0.9, "AGG": 0.1}, [pos("SPY", 20.0, 500.0)], acct(equity=100_000, cash=90_000 - 89_000), {"SPY": 500.0, "AGG": 100.0}, "20260908")
    assert any("scaled" in n for n in plan2.notes)


def test_non_fractionable_rounds_to_whole_shares():
    plan = build_order_plan({"BRK": 0.5}, [], acct(equity=10_000), {"BRK": 3_000.0}, "20260908", fractionable=lambda s: False)
    assert plan.buys[0].qty == 1 and plan.buys[0].notional == 3_000.0
    plan2 = build_order_plan({"BRK": 0.2}, [], acct(equity=10_000), {"BRK": 3_000.0}, "20260908", fractionable=lambda s: False)
    assert plan2.is_empty and dict(plan2.skipped)["BRK"].startswith("non-fractionable")


def test_missing_price_and_short_positions_are_skipped():
    positions = [pos("SHRT", -5, 10.0)]
    plan = build_order_plan({"NOPX": 0.5}, positions, acct(), {"SHRT": 10.0}, "20260908")
    skipped = dict(plan.skipped)
    assert skipped["NOPX"] == "no price" and "short" in skipped["SHRT"]


def test_turnover_cap_blocks_plan():
    plan = build_order_plan({"SPY": 1.0}, [], acct(), {"SPY": 100.0}, "20260908", params=RebalanceParams(max_turnover_pct=0.5))
    assert plan.blocked_reason.startswith("turnover")


def test_min_trade_and_dust_handling():
    positions = [pos("SPY", 0.001, 500.0)]  # $0.50 dust
    plan = build_order_plan({"AGG": 0.5}, positions, acct(equity=100_000, cash=99_999.5), {"SPY": 500.0, "AGG": 100.0}, "20260908")
    assert plan.sells and plan.sells[0].symbol == "SPY" and plan.sells[0].qty == pytest.approx(0.001)
    plan2 = build_order_plan({"SPY": 0.5}, [], acct(equity=100), {"SPY": 500.0}, "20260908", params=RebalanceParams(drift_band_abs=0.0, min_trade_usd=100))
    assert dict(plan2.skipped)["SPY"].startswith("below min trade")


def test_client_ids_attached():
    plan = build_order_plan({"SPY": 0.5}, [], acct(), {"SPY": 500.0}, "20260908", client_id_fn=lambda s, side: f"cp-{s}-{side}")
    assert plan.buys[0].client_order_id == "cp-SPY-buy"


def test_non_positive_equity_blocks():
    plan = build_order_plan({"SPY": 0.5}, [], acct(equity=0, cash=0), {"SPY": 500.0}, "20260908")
    assert plan.blocked_reason and plan.is_empty
