"""Performance statistics on an equity curve (pd.Series indexed by date)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

TRADING_DAYS = 252


@dataclass(frozen=True)
class PerfStats:
    start: str
    end: str
    years: float
    cagr: float
    ann_vol: float
    sharpe: float
    sortino: float
    max_drawdown: float
    calmar: float
    worst_month: float
    best_month: float
    pct_positive_months: float
    total_return: float

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}

    def table(self) -> str:
        rows = [
            ("Period", f"{self.start} → {self.end} ({self.years:.1f}y)"),
            ("CAGR", f"{self.cagr:.2%}"), ("Ann. vol", f"{self.ann_vol:.2%}"), ("Sharpe (rf=0)", f"{self.sharpe:.2f}"),
            ("Sortino", f"{self.sortino:.2f}"), ("Max drawdown", f"{self.max_drawdown:.2%}"), ("Calmar", f"{self.calmar:.2f}"),
            ("Worst month", f"{self.worst_month:.2%}"), ("Best month", f"{self.best_month:.2%}"),
            ("Positive months", f"{self.pct_positive_months:.0%}"), ("Total return", f"{self.total_return:.1%}"),
        ]
        w = max(len(r[0]) for r in rows)
        return "\n".join(f"{k:<{w}}  {v}" for k, v in rows)


def drawdown_series(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return equity / peak - 1.0


def compute_stats(equity: pd.Series, rf_annual: float = 0.0) -> PerfStats:
    eq = equity.dropna()
    if len(eq) < 2:
        raise ValueError("need at least 2 equity points")
    rets = eq.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    total = eq.iloc[-1] / eq.iloc[0] - 1.0
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1.0
    daily_rf = (1 + rf_annual) ** (1 / TRADING_DAYS) - 1
    ex = rets - daily_rf
    vol = rets.std(ddof=1) * np.sqrt(TRADING_DAYS)
    sharpe = float(ex.mean() / ex.std(ddof=1) * np.sqrt(TRADING_DAYS)) if ex.std(ddof=1) > 0 else 0.0
    downside = ex[ex < 0].std(ddof=1) * np.sqrt(TRADING_DAYS)
    sortino = float(ex.mean() * TRADING_DAYS / downside) if downside and downside > 0 else 0.0
    mdd = float(drawdown_series(eq).min())
    monthly = eq.resample("ME").last().pct_change().dropna()
    return PerfStats(
        start=str(eq.index[0].date()), end=str(eq.index[-1].date()), years=float(years), cagr=float(cagr),
        ann_vol=float(vol), sharpe=sharpe, sortino=sortino, max_drawdown=mdd,
        calmar=float(cagr / abs(mdd)) if mdd < 0 else 0.0,
        worst_month=float(monthly.min()) if len(monthly) else 0.0,
        best_month=float(monthly.max()) if len(monthly) else 0.0,
        pct_positive_months=float((monthly > 0).mean()) if len(monthly) else 0.0,
        total_return=float(total),
    )


def yearly_returns(equity: pd.Series) -> pd.Series:
    y = equity.resample("YE").last()
    first = equity.iloc[0]
    out = y.pct_change()
    out.iloc[0] = y.iloc[0] / first - 1.0
    out.index = out.index.year
    return out.rename("return")
