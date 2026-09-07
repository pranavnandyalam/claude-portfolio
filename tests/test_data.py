import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from portfolio import data as d


def _frame(start="2024-01-01", n=300, seed=0, price0=100.0):
    idx = pd.bdate_range(start, periods=n)
    rng = np.random.default_rng(seed)
    close = price0 * np.cumprod(1 + rng.normal(0, 0.01, n))
    return pd.DataFrame({"close": close, "volume": 1_000_000}, index=idx)


@pytest.fixture
def fake_download(monkeypatch):
    calls = []

    def _dl(symbols, start, end):
        calls.append(list(symbols))
        return {s: _frame(seed=i) for i, s in enumerate(symbols) if s != "MISSING"}

    monkeypatch.setattr(d, "_download", _dl)
    return calls


def test_fetch_prices_caches_per_symbol(tmp_path, fake_download):
    pd1 = d.fetch_prices(["SPY", "AGG", "MISSING"], start="2024-01-01", cache_dir=tmp_path)
    assert pd1.symbols == ["SPY", "AGG"]
    assert pd1.missing == ("MISSING",)
    assert pd1.source.startswith("yfinance")
    assert (tmp_path / "prices" / "SPY.csv").exists()
    assert fake_download == [["SPY", "AGG", "MISSING"], ["MISSING"]]  # batch, then one serial retry
    pd2 = d.fetch_prices(["SPY", "AGG"], start="2024-01-01", cache_dir=tmp_path)
    assert pd2.source == "cache"
    assert len(fake_download) == 2
    pd.testing.assert_frame_equal(pd1.close, pd2.close, check_freq=False)


def test_fetch_prices_refetches_when_cache_older_than_max_age(tmp_path, fake_download):
    d.fetch_prices(["SPY"], start="2024-01-01", cache_dir=tmp_path)
    p = tmp_path / "prices" / "SPY.csv"
    old = time.time() - 48 * 3600
    import os
    os.utime(p, (old, old))
    d.fetch_prices(["SPY"], start="2024-01-01", cache_dir=tmp_path, max_age_hours=12)
    assert len(fake_download) == 2


def test_fetch_prices_raises_when_nothing_loads(tmp_path, fake_download):
    with pytest.raises(d.DataError):
        d.fetch_prices(["MISSING"], start="2024-01-01", cache_dir=tmp_path)
    with pytest.raises(d.DataError):
        d.fetch_prices([], cache_dir=tmp_path)


def test_assert_fresh():
    idx = pd.bdate_range("2026-08-01", "2026-09-04")
    close = pd.DataFrame({"SPY": 1.0}, index=idx)
    assert d.assert_fresh(close, today=pd.Timestamp("2026-09-07")) == pd.Timestamp("2026-09-04")
    with pytest.raises(d.DataError):
        d.assert_fresh(close, max_stale_bdays=2, today=pd.Timestamp("2026-09-15"))
    with pytest.raises(d.DataError):
        d.assert_fresh(close.iloc[0:0])


def test_sanity_check_returns_flags_outliers():
    close = pd.DataFrame({"A": [100, 101], "B": [100, 40], "C": [10, 10.1]}, index=pd.bdate_range("2026-01-01", periods=2))
    assert d.sanity_check_returns(close) == ["B"]
    assert d.sanity_check_returns(close.iloc[:1]) == []


def test_trailing_return_and_month_ends():
    idx = pd.bdate_range("2025-01-01", periods=300)
    close = pd.DataFrame({"A": np.linspace(100, 400, 300)}, index=idx)
    tr = d.trailing_return(close, lookback_days=10)
    expected = close["A"].iloc[-1] / close["A"].iloc[-11] - 1
    assert abs(tr["A"] - expected) < 1e-12
    tr_skip = d.trailing_return(close, lookback_days=10, skip_days=2)
    assert abs(tr_skip["A"] - (close["A"].iloc[-3] / close["A"].iloc[-13] - 1)) < 1e-12
    assert np.isnan(d.trailing_return(close, lookback_days=1000)["A"])
    me = d.month_end_dates(idx)
    assert all(m.month != (m + pd.tseries.offsets.BDay(1)).month for m in me[:-1])


def test_fetch_earnings_handles_failure(tmp_path, monkeypatch):
    class Boom:
        def __init__(self, *_):
            pass

        def get_earnings_dates(self, limit=16):
            raise RuntimeError("no network")

    import yfinance
    monkeypatch.setattr(yfinance, "Ticker", Boom)
    df = d.fetch_earnings("AAPL", cache_dir=tmp_path)
    assert list(df.columns) == ["date", "eps_estimate", "reported_eps", "surprise_pct"]
    assert df.empty
