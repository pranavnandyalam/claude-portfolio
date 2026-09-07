import numpy as np
import pandas as pd
import pytest

from portfolio.backtest import buy_and_hold, rebalance_dates, run_backtest
from portfolio.stats import compute_stats, drawdown_series, yearly_returns


def _close(n=800, seed=3):
    idx = pd.bdate_range("2020-01-01", periods=n)
    rng = np.random.default_rng(seed)
    a = 100 * np.cumprod(1 + rng.normal(0.0004, 0.01, n))
    cash = 100 * np.cumprod(1 + np.full(n, 0.0001))
    return pd.DataFrame({"A": a, "CASH": cash}, index=idx)


def test_rebalance_dates_month_ends():
    idx = pd.bdate_range("2025-01-01", "2025-06-30")
    rb = rebalance_dates(idx, "M")
    assert len(rb) == 6 and all(d.month != (d + pd.offsets.BDay(1)).month for d in rb)
    assert len(rebalance_dates(idx, "D")) == len(idx)
    assert len(rebalance_dates(idx, "Q")) == 2
    assert len(rebalance_dates(idx, "W")) >= 25


def test_buy_and_hold_matches_asset_minus_costs():
    close = _close()
    res = run_backtest(close, buy_and_hold("A"), freq="M", cost_bps=0.0, warmup_days=10)
    # strategy decides at first rebalance (month end) then holds; compare from first trade day
    first_trade = res.turnover.index[0]
    ratio_strategy = res.equity.iloc[-1] / res.equity.loc[first_trade]
    ratio_asset = close["A"].iloc[-1] / close["A"].loc[first_trade]
    assert ratio_strategy == pytest.approx(ratio_asset, rel=1e-9)
    assert res.rebalances >= 1 and res.turnover.iloc[0] == 1.0 and res.turnover.iloc[1:].abs().max() == 0.0


def test_costs_reduce_equity_and_cash_symbol_earns():
    close = _close()
    free = run_backtest(close, buy_and_hold("A"), cost_bps=0.0, warmup_days=10)
    paid = run_backtest(close, buy_and_hold("A"), cost_bps=50.0, warmup_days=10)
    assert paid.equity.iloc[-1] < free.equity.iloc[-1]
    idle = run_backtest(close, lambda c: {}, cost_bps=0.0, warmup_days=10)
    assert idle.equity.iloc[-1] == pytest.approx(100_000.0)
    cashy = run_backtest(close, lambda c: {}, cost_bps=0.0, warmup_days=10, cash_symbol="CASH")
    assert cashy.equity.iloc[-1] > 100_000.0


def test_no_lookahead_strategy_receives_only_past_window():
    close = _close()
    seen = []

    def strat(window):
        seen.append(window.index[-1])
        return {"A": 1.0}

    res = run_backtest(close, strat, freq="M", warmup_days=10)
    assert all(d in close.index for d in seen)
    # weights become effective the day AFTER the decision
    first_decision = seen[0]
    pos = close.index.get_loc(first_decision)
    assert res.weights.loc[first_decision, "A"] == 0.0 and res.weights.iloc[pos - 10 + 1]["A"] == 1.0


def test_stats_and_helpers():
    idx = pd.bdate_range("2020-01-01", periods=600)
    eq = pd.Series(np.linspace(100, 200, 600), index=idx)
    st = compute_stats(eq)
    assert st.cagr > 0 and st.max_drawdown == 0.0 and st.sharpe > 0 and st.pct_positive_months == 1.0
    assert "CAGR" in st.table() and st.as_dict()["cagr"] == round(st.cagr, 4)
    dd = drawdown_series(pd.Series([100, 90, 120, 60.0], index=pd.bdate_range("2020-01-01", periods=4)))
    assert dd.min() == pytest.approx(-0.5)
    yr = yearly_returns(eq)
    assert len(yr) >= 2 and yr.iloc[0] > 0
    with pytest.raises(ValueError):
        compute_stats(eq.iloc[:1])
    with pytest.raises(ValueError):
        run_backtest(_close(50), buy_and_hold("A"), warmup_days=100)
