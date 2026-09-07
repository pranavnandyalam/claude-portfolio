"""Faber-style tactical asset allocation (GTAA): hold an asset only when its close is above
its long simple moving average; otherwise that slice goes to the cash proxy.
Reference: Faber (2007/2013) "A Quantitative Approach to Tactical Asset Allocation".
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from portfolio.signals.core import sma_signal


@dataclass(frozen=True)
class TrendParams:
    assets: tuple[str, ...] = ("SPY", "EFA", "IEF", "VNQ", "DBC")
    sma_days: int = 200
    cash: str = "BIL"
    weights: dict[str, float] = field(default_factory=dict)   # empty → equal weight
    min_history_days: int = 210

    def weight_of(self, symbol: str) -> float:
        if self.weights:
            return float(self.weights.get(symbol, 0.0))
        return 1.0 / len(self.assets)


def trend_targets(close: pd.DataFrame, params: TrendParams = TrendParams()) -> dict[str, float]:
    """Return target weights over assets ∪ {cash}. Assets lacking history are treated as
    'off' (cash) rather than dropped, so the allocation stays fully defined."""
    present = [a for a in params.assets if a in close.columns]
    hist = close[present].dropna(how="all")
    if hist.empty or len(hist) < params.min_history_days:
        return {params.cash: 1.0} if params.cash in close.columns else {}
    signal = sma_signal(hist.ffill(), params.sma_days)
    targets: dict[str, float] = {}
    cash_w = 0.0
    for a in params.assets:
        w = params.weight_of(a)
        on = a in signal.index and bool(signal[a]) and pd.notna(hist[a].iloc[-1]) if a in hist.columns else False
        if on:
            targets[a] = targets.get(a, 0.0) + w
        else:
            cash_w += w
    if cash_w > 0 and params.cash in close.columns:
        targets[params.cash] = targets.get(params.cash, 0.0) + cash_w
    return targets
