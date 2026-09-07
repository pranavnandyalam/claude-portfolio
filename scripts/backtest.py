#!/usr/bin/env python
"""Backtest the CONFIGURED portfolio (all enabled sleeves, vol target, costs) and print honest
after-cost stats vs benchmark, yearly returns, and sub-period robustness.
    .venv/bin/python scripts/backtest.py [--start 2008-01-01] [--cost 10] [--freq M] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from portfolio.backtest import buy_and_hold, run_backtest
from portfolio.config import load_config
from portfolio.data import fetch_prices
from portfolio.engine import apply_vol_target, combine_sleeves
from portfolio.stats import yearly_returns


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--start", default="2008-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--cost", type=float, default=10.0)
    ap.add_argument("--freq", default=None, help="M/W/D (default: config schedule.rebalance_freq)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    cfg = load_config(Path(args.config)) if args.config else load_config()
    freq = args.freq or cfg.schedule.rebalance_freq
    close = fetch_prices(cfg.all_symbols, start=cfg.data_start).close

    def strategy(window: pd.DataFrame) -> dict[str, float]:
        combined, _ = combine_sleeves(cfg, window.ffill())
        targets, _, _ = apply_vol_target(cfg, combined, window.ffill())
        return targets

    res = run_backtest(close, strategy, start=args.start, end=args.end, freq=freq, cost_bps=args.cost, warmup_days=270, cash_symbol=cfg.cash_symbol)
    bench = run_backtest(close, buy_and_hold(cfg.benchmark), start=args.start, end=args.end, freq="M", cost_bps=args.cost, warmup_days=270, cash_symbol=cfg.cash_symbol)
    out = {"config": cfg.name, "start": args.start, "end": args.end or str(close.index[-1].date()), "cost_bps": args.cost, "freq": freq,
           "portfolio": res.stats.as_dict(), "benchmark": bench.stats.as_dict(), "avg_annual_turnover": round(res.avg_annual_turnover, 3),
           "yearly": {int(y): {"portfolio": round(float(a), 4), cfg.benchmark: round(float(b), 4)} for (y, a), b in zip(yearly_returns(res.equity).items(), yearly_returns(bench.equity))},
           "subperiods": {}}
    for a, b in (("2008-01-01", "2012-12-31"), ("2013-01-01", "2019-12-31"), ("2020-01-01", None)):
        try:
            r = run_backtest(close, strategy, start=a, end=b, freq=freq, cost_bps=args.cost, warmup_days=270, cash_symbol=cfg.cash_symbol)
            out["subperiods"][f"{a}..{b or 'now'}"] = {"cagr": round(r.stats.cagr, 4), "sharpe": round(r.stats.sharpe, 3), "max_dd": round(r.stats.max_drawdown, 4)}
        except ValueError:
            pass
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"=== {cfg.name}: {out['start']} → {out['end']} | {args.cost:.0f} bps | rebalance {freq} ===")
    print("PORTFOLIO\n" + res.summary())
    print(f"\nBENCHMARK {cfg.benchmark}\n" + bench.stats.table())
    print("\nYearly:")
    for y, d in out["yearly"].items():
        print(f"  {y}: portfolio {d['portfolio']:+.1%}   {cfg.benchmark} {d[cfg.benchmark]:+.1%}")
    print("\nSub-periods:")
    for k, d in out["subperiods"].items():
        print(f"  {k}: CAGR {d['cagr']:+.1%} Sharpe {d['sharpe']:.2f} MaxDD {d['max_dd']:.1%}")
    print("\nCaveats: ETF backtest (no survivorship bias in this universe), monthly signal, trades at close, "
          f"{args.cost:.0f} bps one-way cost, no taxes/dividend timing effects; ~16 strategy variants were examined during design → treat Sharpe as optimistic by ~0.1-0.2.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
