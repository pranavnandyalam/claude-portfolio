"""Honest, after-cost comparison of candidate sleeves on real data. Run:
    .venv/bin/python scripts/research_backtests.py [--start 2008-01-01] [--cost 10]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from portfolio.backtest import buy_and_hold, run_backtest
from portfolio.data import fetch_prices
from portfolio.signals.core import portfolio_realized_vol, vol_target_scalar
from portfolio.signals.dual_momentum import GEMParams, gem_targets
from portfolio.signals.trend import TrendParams, trend_targets
from portfolio.stats import yearly_returns

UNIVERSE = ["SPY", "EFA", "VEU", "IEF", "AGG", "TLT", "VNQ", "DBC", "GLD", "BIL", "SHY", "QQQ", "IWM", "EEM", "TIP"]


def sixty_forty(close):
    return {"SPY": 0.6, "AGG": 0.4}


def gtaa5(close):
    return trend_targets(close, TrendParams(assets=("SPY", "EFA", "IEF", "VNQ", "DBC"), sma_days=200, cash="BIL"))


def gtaa5_gold(close):
    return trend_targets(close, TrendParams(assets=("SPY", "EFA", "IEF", "VNQ", "GLD"), sma_days=200, cash="BIL"))


def gem(close):
    return gem_targets(close, GEMParams(us="SPY", intl="VEU", bonds="AGG", tbill="BIL", lookback_days=252))


def gem_smooth(close):
    return gem_targets(close, GEMParams(us="SPY", intl="VEU", bonds="AGG", tbill="BIL", horizons=(63, 126, 252)))


def blend(close):
    """50% GTAA-5 (gold variant) + 50% GEM — two diversified, low-correlation rule sets."""
    a, b = gtaa5_gold(close), gem(close)
    out: dict[str, float] = {}
    for src, w in ((a, 0.5), (b, 0.5)):
        for s, x in src.items():
            out[s] = out.get(s, 0.0) + w * x
    return out


def blend_voltarget(close, target=0.10):
    w = blend(close)
    risky = {s: x for s, x in w.items() if s not in ("BIL", "SHY")}
    pv = portfolio_realized_vol(close, risky, window=63)
    k = vol_target_scalar(pv, target, cap=1.0)
    scaled = {s: x * k for s, x in risky.items()}
    cash = 1.0 - sum(scaled.values())
    if cash > 0:
        scaled["BIL"] = scaled.get("BIL", 0.0) + cash
    return scaled


STRATS = {
    "SPY buy&hold": buy_and_hold("SPY"),
    "60/40 (SPY/AGG)": sixty_forty,
    "GTAA-5 (Faber, 200d SMA)": gtaa5,
    "GTAA-5 gold variant": gtaa5_gold,
    "GEM (Antonacci 12m)": gem,
    "GEM smoothed (3/6/12m)": gem_smooth,
    "Blend 50/50 GTAA+GEM": blend,
    "Blend + 10% vol target": blend_voltarget,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2008-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--cost", type=float, default=10.0, help="one-way cost in bps per $ traded")
    ap.add_argument("--freq", default="M")
    args = ap.parse_args()
    data = fetch_prices(UNIVERSE, start="2003-01-01")
    close = data.close
    print(f"data: {close.index[0].date()} → {close.index[-1].date()}, symbols={len(close.columns)}, missing={data.missing}")
    rows = []
    results = {}
    for name, strat in STRATS.items():
        res = run_backtest(close, strat, start=args.start, end=args.end, freq=args.freq, cost_bps=args.cost, warmup_days=260, cash_symbol="BIL")
        results[name] = res
        s = res.stats
        rows.append({"strategy": name, "CAGR": s.cagr, "Vol": s.ann_vol, "Sharpe": s.sharpe, "MaxDD": s.max_drawdown,
                     "Calmar": s.calmar, "WorstMo": s.worst_month, "Turnover/yr": res.avg_annual_turnover})
    df = pd.DataFrame(rows).set_index("strategy")
    pd.set_option("display.width", 200)
    print(f"\n=== {args.start} → {args.end or close.index[-1].date()} | cost {args.cost:.0f} bps one-way | rebalance {args.freq} ===")
    print(df.to_string(formatters={"CAGR": "{:.1%}".format, "Vol": "{:.1%}".format, "Sharpe": "{:.2f}".format,
                                   "MaxDD": "{:.1%}".format, "Calmar": "{:.2f}".format, "WorstMo": "{:.1%}".format, "Turnover/yr": "{:.0%}".format}))
    yr = pd.DataFrame({n: yearly_returns(r.equity) for n, r in results.items()})
    print("\nYearly returns:")
    print(yr.to_string(float_format=lambda x: f"{x:+.1%}"))
    # sub-period robustness
    for a, b in (("2008-01-01", "2012-12-31"), ("2013-01-01", "2019-12-31"), ("2020-01-01", None)):
        sub = []
        for name, strat in STRATS.items():
            r = run_backtest(close, strat, start=a, end=b, freq=args.freq, cost_bps=args.cost, warmup_days=260, cash_symbol="BIL")
            sub.append(f"{name[:22]:22s} CAGR {r.stats.cagr:+.1%} Sharpe {r.stats.sharpe:.2f} MaxDD {r.stats.max_drawdown:.1%}")
        print(f"\n--- {a} → {b or 'now'} ---\n" + "\n".join(sub))


if __name__ == "__main__":
    main()
