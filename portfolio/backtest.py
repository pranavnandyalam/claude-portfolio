"""Daily-resolution backtester for long-only target-weight strategies.

- `strategy(close_window) -> {symbol: weight}` is called on each rebalance date with ONLY
  data up to and including that date (no look-ahead). Weights apply from the NEXT day's
  close-to-close return (trade at next close ≈ conservative for daily signals).
- Costs: `cost_bps` per unit of turnover (one-way), applied on rebalance days.
- Cash earns `cash_symbol` returns if given (e.g. BIL), else 0.
- Returns equity curve, weights history, turnover series and PerfStats.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from portfolio.stats import PerfStats, compute_stats

Strategy = Callable[[pd.DataFrame], dict[str, float]]


@dataclass(frozen=True)
class BacktestResult:
    equity: pd.Series
    weights: pd.DataFrame
    turnover: pd.Series
    stats: PerfStats
    rebalances: int
    avg_annual_turnover: float
    cost_bps: float

    def summary(self) -> str:
        return (f"{self.stats.table()}\nRebalances       {self.rebalances}\n"
                f"Avg turnover/yr  {self.avg_annual_turnover:.0%} (one-way, cost {self.cost_bps:.0f} bps)")


def rebalance_dates(index: pd.DatetimeIndex, freq: str = "M", day_offset: int = 0) -> pd.DatetimeIndex:
    """Last trading day of each period ('M' month, 'W' week, 'Q' quarter, 'D' every day),
    optionally shifted forward by `day_offset` trading days (clamped inside the index)."""
    if freq == "D":
        return index
    keys = {"M": [index.year, index.month], "Q": [index.year, index.quarter], "W": [index.isocalendar().year.values, index.isocalendar().week.values]}[freq]
    s = pd.Series(np.arange(len(index)), index=index)
    last_pos = s.groupby(keys).last().values
    pos = np.clip(last_pos + day_offset, 0, len(index) - 1)
    return pd.DatetimeIndex(index[np.unique(pos)])


def run_backtest(
    close: pd.DataFrame,
    strategy: Strategy,
    start: str | None = None,
    end: str | None = None,
    freq: str = "M",
    cost_bps: float = 10.0,
    warmup_days: int = 260,
    cash_symbol: str | None = None,
    initial_equity: float = 100_000.0,
    day_offset: int = 0,
) -> BacktestResult:
    close = close.sort_index()
    if end is not None:
        close = close.loc[: pd.Timestamp(end)]
    rets = close.pct_change()
    idx = close.index
    first_pos = warmup_days
    if start is not None:
        first_pos = max(first_pos, idx.searchsorted(pd.Timestamp(start)))
    if first_pos >= len(idx) - 2:
        raise ValueError("not enough data after warmup/start")
    rb = set(rebalance_dates(idx[first_pos:], freq, day_offset))

    equity = np.empty(len(idx) - first_pos)
    weights_hist: dict[pd.Timestamp, dict[str, float]] = {}
    turnover: dict[pd.Timestamp, float] = {}
    w: dict[str, float] = {}          # weights in force for today's return
    pending: dict[str, float] | None = None
    eq = initial_equity
    cost = cost_bps / 10_000.0
    n_rb = 0

    for k, pos in enumerate(range(first_pos, len(idx))):
        date = idx[pos]
        # 1) apply pending rebalance decided at previous close (trade at this close → effective tomorrow)
        if pending is not None:
            all_syms = set(w) | set(pending)
            traded = sum(abs(pending.get(s, 0.0) - w.get(s, 0.0)) for s in all_syms)  # $ traded / NAV
            d_cash = abs((1.0 - sum(pending.values())) - (1.0 - sum(w.values())))
            eq *= 1.0 - traded * cost            # cost per dollar traded (cash moves are free)
            turnover[date] = (traded + d_cash) / 2.0  # one-way turnover incl. cash leg
            w = pending
            pending = None
            n_rb += 1
        # 2) today's return with weights in force
        r_today = rets.iloc[pos]
        port_ret = 0.0
        invested = 0.0
        for s, ws in w.items():
            r = r_today.get(s, np.nan)
            if ws > 0 and np.isfinite(r):
                port_ret += ws * r
                invested += ws
        cash_w = max(0.0, 1.0 - invested)
        if cash_symbol and cash_symbol in r_today and np.isfinite(r_today[cash_symbol]):
            port_ret += cash_w * r_today[cash_symbol]
        eq *= 1.0 + port_ret
        equity[k] = eq
        weights_hist[date] = w
        # 3) decide new weights at this close if rebalance date
        if date in rb:
            window = close.iloc[: pos + 1]
            target = strategy(window)
            target = {s: float(x) for s, x in target.items() if x and np.isfinite(x) and x > 0}
            tot = sum(target.values())
            if tot > 1.0 + 1e-9:
                target = {s: x / tot for s, x in target.items()}
            pending = target

    eq_series = pd.Series(equity, index=idx[first_pos:], name="equity")
    w_df = pd.DataFrame.from_dict(weights_hist, orient="index").reindex(eq_series.index).fillna(0.0)
    to_series = pd.Series(turnover, name="turnover").sort_index()
    years = max((eq_series.index[-1] - eq_series.index[0]).days / 365.25, 1e-9)
    return BacktestResult(
        equity=eq_series, weights=w_df, turnover=to_series, stats=compute_stats(eq_series),
        rebalances=n_rb, avg_annual_turnover=float(to_series.sum() / years) if len(to_series) else 0.0, cost_bps=cost_bps,
    )


def buy_and_hold(symbol: str) -> Strategy:
    return lambda close: {symbol: 1.0}
