import numpy as np
import pandas as pd

from portfolio.signals.rotation import RotationParams, rotation_targets


def _close(n=400):
    idx = pd.bdate_range("2024-01-01", periods=n)
    rng = np.random.default_rng(7)
    cols = {}
    for i, (name, drift, vol) in enumerate([("A", 0.003, 0.01), ("B", 0.002, 0.02), ("C", 0.001, 0.01), ("D", -0.003, 0.02), ("BIL", 0.0001, 0.0005)]):
        cols[name] = 100 * np.cumprod(1 + rng.normal(drift, vol, n))
    return pd.DataFrame(cols, index=idx)


def test_rotation_picks_top_n_above_sma_with_inverse_vol():
    close = _close()
    p = RotationParams(universe=("A", "B", "C", "D"), top_n=2, cash="BIL")
    t = rotation_targets(close, p)
    assert set(t) <= {"A", "B", "C"} and "D" not in t and abs(sum(t.values()) - 1) < 1e-9
    assert len([s for s in t if s != "BIL"]) == 2
    chosen = [s for s in t if s != "BIL"]
    if "A" in chosen and "B" in chosen:
        assert t["A"] > t["B"]  # lower vol gets more weight
    p_eq = RotationParams(universe=("A", "B", "C", "D"), top_n=2, cash="BIL", inverse_vol=False)
    t_eq = rotation_targets(close, p_eq)
    assert all(abs(w - 0.5) < 1e-9 for w in t_eq.values())


def test_rotation_falls_back_to_cash():
    close = _close()
    assert rotation_targets(close.iloc[:100], RotationParams(universe=("A", "B"), cash="BIL")) == {"BIL": 1.0}
    only_down = RotationParams(universe=("D",), top_n=1, cash="BIL")
    assert rotation_targets(close, only_down) == {"BIL": 1.0}
    partial = RotationParams(universe=("A", "D", "D2"), top_n=2, cash="BIL")
    t = rotation_targets(close, partial)
    assert "A" in t and t.get("BIL", 0) > 0.4  # one empty slot → cash
