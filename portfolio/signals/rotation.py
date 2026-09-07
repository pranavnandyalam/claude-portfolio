"""Momentum ETF rotation with a trend filter (Adaptive-Asset-Allocation style, Butler/Philbrick/
Gordillo 2012): rank a small ETF universe by composite momentum, hold the top N that are also
above their long SMA, weight by inverse volatility (or equal), remainder to cash.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from portfolio.signals.core import inverse_vol_weights, multi_horizon_momentum, realized_vol, sma_signal


@dataclass(frozen=True)
class RotationParams:
    universe: tuple[str, ...] = ("SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "TLT", "IEF", "GLD", "DBC")
    top_n: int = 3
    horizons: tuple[int, ...] = (63, 126, 252)
    sma_days: int = 200
    cash: str = "BIL"
    vol_window: int = 63
    inverse_vol: bool = True
    require_positive_momentum: bool = True
    min_history_days: int = 260


def rotation_targets(close: pd.DataFrame, params: RotationParams = RotationParams()) -> dict[str, float]:
    present = [s for s in params.universe if s in close.columns]
    hist = close[present].dropna(how="all").ffill()
    if hist.empty or len(hist) < params.min_history_days:
        return {params.cash: 1.0} if params.cash in close.columns else {}
    mom = multi_horizon_momentum(hist, params.horizons).dropna()
    above = sma_signal(hist, params.sma_days)
    eligible = [s for s in mom.sort_values(ascending=False).index if bool(above.get(s, False)) and (mom[s] > 0 or not params.require_positive_momentum)]
    chosen = eligible[: params.top_n]
    slot = 1.0 / params.top_n
    if not chosen:
        return {params.cash: 1.0} if params.cash in close.columns else {}
    if params.inverse_vol:
        vol = realized_vol(hist[chosen], params.vol_window)
        w = inverse_vol_weights(vol)
        targets = {s: float(w[s]) * slot * len(chosen) for s in chosen if s in w.index}
    else:
        targets = {s: slot for s in chosen}
    cash_w = max(0.0, 1.0 - sum(targets.values()))
    if cash_w > 1e-9 and params.cash in close.columns:
        targets[params.cash] = targets.get(params.cash, 0.0) + cash_w
    return targets
