import numpy as np
import pandas as pd
import pytest

from portfolio.signals import core as c


def _close(n=400, seed=1):
    idx = pd.bdate_range("2024-01-01", periods=n)
    rng = np.random.default_rng(seed)
    a = 100 * np.cumprod(1 + rng.normal(0.0005, 0.01, n))
    b = 100 * np.cumprod(1 + rng.normal(-0.0005, 0.02, n))
    return pd.DataFrame({"A": a, "B": b}, index=idx)


def test_total_return_and_rank():
    close = _close()
    tr = c.total_return(close, 252, 21)
    assert abs(tr["A"] - (close["A"].iloc[-22] / close["A"].iloc[-274] - 1)) < 1e-12
    assert np.isnan(c.total_return(close, 1000)["A"])
    rank = c.momentum_rank(close)
    assert list(rank.index) == ["A", "B"]


def test_sma_signal_and_vol():
    close = _close()
    sig = c.sma_signal(close, 200)
    assert sig.dtype == bool and set(sig.index) == {"A", "B"}
    assert not c.sma_signal(close.iloc[:10], 200).any()
    vol = c.realized_vol(close, 63)
    assert vol["B"] > vol["A"] and 0.05 < vol["A"] < 0.4
    assert np.isnan(c.realized_vol(close.iloc[:10], 63)["A"])


def test_inverse_vol_weights_and_vol_target():
    w = c.inverse_vol_weights(pd.Series({"A": 0.10, "B": 0.20, "C": np.nan}))
    assert abs(w.sum() - 1) < 1e-12 and abs(w["A"] / w["B"] - 2.0) < 1e-12 and "C" not in w
    assert c.inverse_vol_weights(pd.Series(dtype=float)).empty
    assert c.vol_target_scalar(0.20, 0.10) == 0.5
    assert c.vol_target_scalar(0.05, 0.10) == 1.0  # no leverage
    assert c.vol_target_scalar(float("nan"), 0.10) == 1.0


def test_multi_horizon_and_portfolio_vol():
    close = _close()
    m = c.multi_horizon_momentum(close)
    assert set(m.index) == {"A", "B"} and np.isfinite(m).all()
    pv = c.portfolio_realized_vol(close, {"A": 0.5, "B": 0.5})
    assert 0.05 < pv < 0.5
    assert np.isnan(c.portfolio_realized_vol(close, {"Z": 1.0}))
