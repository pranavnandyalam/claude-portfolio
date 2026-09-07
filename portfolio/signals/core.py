"""Pure signal primitives on adjusted-close frames. No I/O, no mutation, no look-ahead:
every function only uses rows <= the last row of the frame it is given.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def total_return(close: pd.DataFrame, lookback: int, skip: int = 0) -> pd.Series:
    """P[t-skip]/P[t-skip-lookback] - 1 (row offsets). NaN if history insufficient."""
    if len(close) <= lookback + skip:
        return pd.Series(np.nan, index=close.columns, name="total_return")
    return (close.iloc[-1 - skip] / close.iloc[-1 - skip - lookback] - 1.0).rename("total_return")


def sma_signal(close: pd.DataFrame, window: int) -> pd.Series:
    """True where last close > simple moving average of the last `window` closes (Faber-style)."""
    if len(close) < window:
        return pd.Series(False, index=close.columns, name="above_sma")
    sma = close.iloc[-window:].mean()
    return (close.iloc[-1] > sma).rename("above_sma")


def realized_vol(close: pd.DataFrame, window: int = 63, annualize: bool = True) -> pd.Series:
    """Std of daily log returns over the last `window` rows, annualized by default."""
    if len(close) < window + 1:
        return pd.Series(np.nan, index=close.columns, name="vol")
    rets = np.log(close.iloc[-window - 1:]).diff().iloc[1:]
    vol = rets.std(ddof=1)
    return (vol * np.sqrt(TRADING_DAYS) if annualize else vol).rename("vol")


def inverse_vol_weights(vol: pd.Series, floor: float = 0.02) -> pd.Series:
    """Weights proportional to 1/vol over non-NaN entries, summing to 1 (empty if none)."""
    v = vol.dropna().clip(lower=floor)
    if v.empty:
        return pd.Series(dtype=float)
    inv = 1.0 / v
    return (inv / inv.sum()).rename("weight")


def momentum_rank(close: pd.DataFrame, lookback: int = 252, skip: int = 21) -> pd.Series:
    """Cross-sectional 12-1 style momentum score (higher = stronger), NaN dropped."""
    return total_return(close, lookback, skip).dropna().sort_values(ascending=False)


def multi_horizon_momentum(close: pd.DataFrame, horizons: tuple[int, ...] = (63, 126, 252)) -> pd.Series:
    """Average of total returns over several horizons (Antonacci/AllocateSmartly 'composite')."""
    parts = [total_return(close, h) for h in horizons]
    return pd.concat(parts, axis=1).mean(axis=1, skipna=False).rename("composite_momentum")


def vol_target_scalar(portfolio_vol: float, target: float, cap: float = 1.0, floor: float = 0.0) -> float:
    """Exposure multiplier to hit `target` annual vol; clipped to [floor, cap] (no leverage by default)."""
    if portfolio_vol is None or not np.isfinite(portfolio_vol) or portfolio_vol <= 0:
        return cap
    return float(np.clip(target / portfolio_vol, floor, cap))


def portfolio_realized_vol(close: pd.DataFrame, weights: dict[str, float], window: int = 63) -> float:
    """Annualized realized vol of a fixed-weight portfolio over the last `window` days."""
    syms = [s for s in weights if s in close.columns and weights[s] > 0]
    if not syms or len(close) < window + 1:
        return float("nan")
    rets = close[syms].iloc[-window - 1:].pct_change().iloc[1:]
    w = np.array([weights[s] for s in syms])
    pr = rets.fillna(0.0).values @ w
    return float(np.std(pr, ddof=1) * np.sqrt(TRADING_DAYS))
