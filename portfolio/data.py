"""Market data layer: adjusted daily prices + earnings surprises via yfinance, with a
per-symbol on-disk cache so repeated daily runs are cheap and offline-tolerant.

Design notes
- All frames are tz-naive, indexed by calendar date (pd.Timestamp at midnight).
- Prices are total-return adjusted closes (auto_adjust=True) — correct for signals.
- Nothing here mutates inputs; every function returns new objects.
- Never raises on a single bad symbol; missing symbols are reported in PriceData.missing.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE = PROJECT_ROOT / "state" / "cache"
PRICE_CACHE_MAX_AGE_HOURS = 12
EARNINGS_CACHE_MAX_AGE_HOURS = 24
DOWNLOAD_RETRIES = 3
RETRY_SLEEP_SECONDS = 2.0


class DataError(RuntimeError):
    """Raised when data is unusable (empty, stale, or malformed)."""


@dataclass(frozen=True)
class PriceData:
    close: pd.DataFrame
    volume: pd.DataFrame
    source: str
    fetched_at: datetime
    missing: tuple[str, ...] = field(default_factory=tuple)

    @property
    def symbols(self) -> list[str]:
        return list(self.close.columns)

    @property
    def last_date(self) -> pd.Timestamp:
        return pd.Timestamp(self.close.index[-1])


# --------------------------------------------------------------------------- utils

def _configure_yf_cache(cache_dir: Path) -> None:
    """Point yfinance's sqlite tz-cache at a project-local dir (avoids 'database is locked'
    collisions with other processes on the shared default cache)."""
    try:
        import yfinance as yf

        tz_dir = cache_dir / "yf_tz"
        tz_dir.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(tz_dir))
    except Exception as exc:  # pragma: no cover - best effort
        log.debug("could not set yfinance tz cache: %s", exc)


def _naive_dates(index: pd.Index) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_convert(None)
    return idx.normalize()


def _cache_path(cache_dir: Path, symbol: str) -> Path:
    safe = symbol.replace("/", "_").replace("^", "_")
    return cache_dir / "prices" / f"{safe}.csv"


def _read_cached(cache_dir: Path, symbol: str, max_age_hours: float) -> pd.DataFrame | None:
    path = _cache_path(cache_dir, symbol)
    if not path.exists():
        return None
    age_hours = (time.time() - path.stat().st_mtime) / 3600.0
    if age_hours > max_age_hours:
        return None
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
    except Exception as exc:
        log.warning("corrupt cache for %s (%s); refetching", symbol, exc)
        return None
    if df.empty or "close" not in df.columns:
        return None
    df.index = _naive_dates(df.index)
    return df


def _write_cache(cache_dir: Path, symbol: str, df: pd.DataFrame) -> None:
    path = _cache_path(cache_dir, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.to_csv(tmp)
    tmp.replace(path)


def _download(symbols: list[str], start: str, end: str | None) -> dict[str, pd.DataFrame]:
    """Download adjusted OHLCV for symbols. Returns {symbol: DataFrame[close, volume]}."""
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    last_exc: Exception | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            raw = yf.download(
                symbols,
                start=start,
                end=end,
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=attempt == 1,  # fall back to serial on retry
                group_by="ticker",
            )
            break
        except Exception as exc:  # network / sqlite lock / etc.
            last_exc = exc
            log.warning("yfinance download attempt %d failed: %s", attempt, exc)
            time.sleep(RETRY_SLEEP_SECONDS * attempt)
    else:
        raise DataError(f"yfinance download failed after {DOWNLOAD_RETRIES} attempts: {last_exc}")

    if raw is None or raw.empty:
        return out
    if len(symbols) == 1 and not isinstance(raw.columns, pd.MultiIndex):
        raw = pd.concat({symbols[0]: raw}, axis=1)
    for sym in symbols:
        if sym not in raw.columns.get_level_values(0):
            continue
        sub = raw[sym]
        if "Close" not in sub.columns:
            continue
        df = pd.DataFrame({"close": sub["Close"], "volume": sub.get("Volume")})
        df.index = _naive_dates(df.index)
        df = df[~df.index.duplicated(keep="last")].dropna(subset=["close"])
        if not df.empty:
            out[sym] = df
    return out


# --------------------------------------------------------------------------- public API

def fetch_prices(
    symbols: list[str],
    start: str = "2000-01-01",
    end: str | None = None,
    cache_dir: Path = DEFAULT_CACHE,
    max_age_hours: float = PRICE_CACHE_MAX_AGE_HOURS,
    force_refresh: bool = False,
) -> PriceData:
    """Fetch adjusted daily closes/volumes for symbols with per-symbol caching.

    Symbols that cannot be fetched are omitted and listed in PriceData.missing.
    Raises DataError only if *nothing* could be loaded.
    """
    symbols = list(dict.fromkeys(s.strip().upper() for s in symbols if s and s.strip()))
    if not symbols:
        raise DataError("no symbols requested")
    cache_dir.mkdir(parents=True, exist_ok=True)
    _configure_yf_cache(cache_dir)

    frames: dict[str, pd.DataFrame] = {}
    to_fetch: list[str] = []
    for sym in symbols:
        cached = None if force_refresh else _read_cached(cache_dir, sym, max_age_hours)
        if cached is not None and pd.Timestamp(cached.index[0]) <= pd.Timestamp(start) + pd.Timedelta(days=7):
            frames[sym] = cached
        else:
            to_fetch.append(sym)

    source = "cache"
    if to_fetch:
        fetched = _download(to_fetch, start, end)
        still_missing = [s for s in to_fetch if s not in fetched]
        if still_missing:  # transient per-symbol failures are common; retry them serially once
            time.sleep(RETRY_SLEEP_SECONDS)
            for sym in still_missing:
                try:
                    fetched = {**fetched, **_download([sym], start, end)}
                except DataError as exc:
                    log.warning("retry failed for %s: %s", sym, exc)
        for sym, df in fetched.items():
            _write_cache(cache_dir, sym, df)
            frames[sym] = df
        source = "yfinance" if not frames.keys() - set(fetched) else "yfinance+cache"

    missing = tuple(s for s in symbols if s not in frames)
    if not frames:
        raise DataError(f"no price data for any of {symbols}")
    if missing:
        log.warning("missing price data for: %s", ", ".join(missing))

    close = pd.concat({s: f["close"] for s, f in frames.items()}, axis=1).sort_index()
    volume = pd.concat({s: f["volume"] for s, f in frames.items()}, axis=1).sort_index()
    close = close.loc[pd.Timestamp(start):]
    volume = volume.loc[pd.Timestamp(start):]
    if end is not None:
        close = close.loc[: pd.Timestamp(end)]
        volume = volume.loc[: pd.Timestamp(end)]
    return PriceData(close=close, volume=volume, source=source, fetched_at=datetime.now(timezone.utc), missing=missing)


def assert_fresh(close: pd.DataFrame, max_stale_bdays: int = 3, today: pd.Timestamp | None = None) -> pd.Timestamp:
    """Raise DataError if the last available bar is older than max_stale_bdays business days.
    Returns the last bar date. Uses business days (no holiday calendar) → slightly permissive
    around holidays, which is the safe direction for a staleness check."""
    if close.empty:
        raise DataError("empty price frame")
    today = pd.Timestamp(today or pd.Timestamp.now(tz="America/New_York").tz_localize(None)).normalize()
    last = pd.Timestamp(close.index[-1]).normalize()
    stale_bdays = len(pd.bdate_range(last, today)) - 1
    if stale_bdays > max_stale_bdays:
        raise DataError(f"price data stale: last bar {last.date()} is {stale_bdays} business days before {today.date()}")
    return last


def sanity_check_returns(close: pd.DataFrame, max_abs_daily_return: float = 0.5) -> list[str]:
    """Return symbols whose most recent daily move exceeds max_abs_daily_return (likely bad
    data or a corporate action we should not trade on blindly)."""
    if len(close) < 2:
        return []
    last_ret = (close.iloc[-1] / close.iloc[-2] - 1.0).abs()
    return sorted(last_ret[last_ret > max_abs_daily_return].index.tolist())


def fetch_earnings(symbol: str, cache_dir: Path = DEFAULT_CACHE, limit: int = 16,
                   max_age_hours: float = EARNINGS_CACHE_MAX_AGE_HOURS) -> pd.DataFrame:
    """Earnings dates with EPS estimate/actual/surprise% for a symbol (past + upcoming).
    Columns: date (naive), eps_estimate, reported_eps, surprise_pct. Empty frame on failure."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "earnings" / f"{symbol.upper()}.csv"
    if path.exists() and (time.time() - path.stat().st_mtime) / 3600.0 <= max_age_hours:
        try:
            df = pd.read_csv(path, parse_dates=["date"])
            return df
        except Exception:
            pass
    try:
        import yfinance as yf

        _configure_yf_cache(cache_dir)
        raw = yf.Ticker(symbol).get_earnings_dates(limit=limit)
    except Exception as exc:
        log.warning("earnings fetch failed for %s: %s", symbol, exc)
        return pd.DataFrame(columns=["date", "eps_estimate", "reported_eps", "surprise_pct"])
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["date", "eps_estimate", "reported_eps", "surprise_pct"])
    df = pd.DataFrame({
        "date": _naive_dates(raw.index),
        "eps_estimate": pd.to_numeric(raw.get("EPS Estimate"), errors="coerce").values,
        "reported_eps": pd.to_numeric(raw.get("Reported EPS"), errors="coerce").values,
        "surprise_pct": pd.to_numeric(raw.get("Surprise(%)"), errors="coerce").values,
    }).sort_values("date").reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return df


def fetch_sp500_constituents(cache_dir: Path = DEFAULT_CACHE, max_age_hours: float = 24 * 7) -> list[str]:
    """Current S&P 500 tickers from Wikipedia (cached weekly). NOTE: this is the *current*
    list → any backtest on it has survivorship bias. Use only for live universe selection
    and label backtests accordingly."""
    path = cache_dir / "sp500.txt"
    if path.exists() and (time.time() - path.stat().st_mtime) / 3600.0 <= max_age_hours:
        return [l.strip() for l in path.read_text().splitlines() if l.strip()]
    import requests

    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    resp = requests.get(url, headers={"User-Agent": "claude-portfolio/0.1"}, timeout=30)
    resp.raise_for_status()
    from io import StringIO

    tables = pd.read_html(StringIO(resp.text))
    table = next(t for t in tables if "Symbol" in t.columns)
    syms = sorted({str(s).replace(".", "-").strip().upper() for s in table["Symbol"].tolist()})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(syms) + "\n")
    return syms


def month_end_dates(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Last available trading day of each month in index."""
    s = pd.Series(index, index=index)
    return pd.DatetimeIndex(s.groupby([index.year, index.month]).last().values)


def trailing_return(close: pd.DataFrame, lookback_days: int, skip_days: int = 0) -> pd.Series:
    """(P[t-skip] / P[t-skip-lookback]) - 1 for each column, using row offsets."""
    if len(close) <= lookback_days + skip_days:
        return pd.Series(np.nan, index=close.columns)
    end = close.iloc[-1 - skip_days]
    start = close.iloc[-1 - skip_days - lookback_days]
    return (end / start - 1.0).rename("trailing_return")
