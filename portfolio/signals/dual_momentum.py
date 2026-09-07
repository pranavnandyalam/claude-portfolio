"""Antonacci Global Equities Momentum (GEM): absolute momentum gate (vs T-bills) + relative
momentum between US and ex-US equities; bonds when equities fail the absolute test.
Reference: Antonacci (2014) "Dual Momentum Investing".
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from portfolio.signals.core import total_return


@dataclass(frozen=True)
class GEMParams:
    us: str = "SPY"
    intl: str = "VEU"
    bonds: str = "AGG"
    tbill: str = "BIL"
    lookback_days: int = 252
    # Optional smoothing: average of several lookbacks (0 = single lookback)
    horizons: tuple[int, ...] = ()


def _score(close: pd.DataFrame, sym: str, params: GEMParams) -> float | None:
    if sym not in close.columns:
        return None
    s = close[[sym]].dropna()
    if params.horizons:
        vals = [total_return(s, h)[sym] for h in params.horizons]
        vals = [v for v in vals if pd.notna(v)]
        return float(sum(vals) / len(vals)) if vals else None
    v = total_return(s, params.lookback_days)[sym]
    return None if pd.isna(v) else float(v)


def gem_targets(close: pd.DataFrame, params: GEMParams = GEMParams()) -> dict[str, float]:
    us, intl, tb = (_score(close, params.us, params), _score(close, params.intl, params), _score(close, params.tbill, params))
    if us is None:
        return {params.bonds: 1.0} if params.bonds in close.columns else {}
    hurdle = tb if tb is not None else 0.0
    if us > hurdle:
        if intl is not None and intl > us:
            return {params.intl: 1.0}
        return {params.us: 1.0}
    if params.bonds in close.columns:
        return {params.bonds: 1.0}
    return {params.tbill: 1.0} if params.tbill in close.columns else {}
