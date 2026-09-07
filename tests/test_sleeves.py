import numpy as np
import pandas as pd

from portfolio.signals.dual_momentum import GEMParams, gem_targets
from portfolio.signals.trend import TrendParams, trend_targets


def _series(n, drift, seed):
    rng = np.random.default_rng(seed)
    return 100 * np.cumprod(1 + rng.normal(drift, 0.01, n))


def _close(n=400):
    idx = pd.bdate_range("2024-01-01", periods=n)
    return pd.DataFrame({"UP": _series(n, 0.002, 1), "DOWN": _series(n, -0.002, 2), "BIL": _series(n, 0.0001, 3),
                         "AGG": _series(n, 0.0001, 4), "INTL": _series(n, 0.001, 5)}, index=idx)


def test_trend_targets_puts_downtrend_in_cash():
    close = _close()
    t = trend_targets(close, TrendParams(assets=("UP", "DOWN"), sma_days=200, cash="BIL"))
    assert t == {"UP": 0.5, "BIL": 0.5}
    t2 = trend_targets(close, TrendParams(assets=("UP", "DOWN"), sma_days=200, cash="BIL", weights={"UP": 0.7, "DOWN": 0.3}))
    assert t2 == {"UP": 0.7, "BIL": 0.3}
    assert trend_targets(close.iloc[:50], TrendParams(assets=("UP",), cash="BIL")) == {"BIL": 1.0}
    t3 = trend_targets(close, TrendParams(assets=("UP", "MISSING"), cash="BIL"))
    assert t3 == {"UP": 0.5, "BIL": 0.5}
    assert abs(sum(t.values()) - 1) < 1e-12


def test_gem_targets_branches():
    close = _close()
    p = GEMParams(us="UP", intl="INTL", bonds="AGG", tbill="BIL", lookback_days=252)
    assert gem_targets(close, p) == {"UP": 1.0}
    p2 = GEMParams(us="DOWN", intl="INTL", bonds="AGG", tbill="BIL", lookback_days=252)
    assert gem_targets(close, p2) == {"AGG": 1.0}  # US fails the absolute-momentum gate → bonds
    p3 = GEMParams(us="DOWN", intl="DOWN", bonds="AGG", tbill="BIL", lookback_days=252)
    assert gem_targets(close, p3) == {"AGG": 1.0}
    p4 = GEMParams(us="INTL", intl="UP", bonds="AGG", tbill="BIL", lookback_days=252)
    assert gem_targets(close, p4) == {"UP": 1.0}
    assert gem_targets(close.iloc[:100], p) == {"AGG": 1.0}
    p5 = GEMParams(us="UP", intl="INTL", bonds="AGG", tbill="BIL", horizons=(63, 126, 252))
    assert gem_targets(close, p5) == {"UP": 1.0}
