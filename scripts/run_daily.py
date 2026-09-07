#!/usr/bin/env python
"""Daily engine entry point. Safe by default (dry-run). Examples:
    .venv/bin/python scripts/run_daily.py --preflight
    .venv/bin/python scripts/run_daily.py                # dry-run: prints the plan, places nothing
    .venv/bin/python scripts/run_daily.py --execute      # places orders on the PAPER account
    .venv/bin/python scripts/run_daily.py --reset-kill-switch --reason "reviewed drawdown"
Exit codes: 0 ok, 2 halted/blocked, 3 error, 4 lock held.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio import state as st
from portfolio.broker import AlpacaBroker, AuthError, BrokerError, FakeBroker, LiveTradingBlocked
from portfolio.config import ConfigError, load_config
from portfolio.data import fetch_prices
from portfolio.engine import run


def preflight(cfg) -> int:
    print(f"config: {cfg.name} mode={cfg.mode} sleeves={[s.name for s in cfg.enabled_sleeves]} cash={cfg.cash_symbol}")
    ks = st.read_kill_switch()
    print(f"kill switch: {'ACTIVE — ' + ks.reason if ks.active else 'off'}")
    try:
        pdata = fetch_prices(cfg.all_symbols, start=cfg.data_start)
        print(f"data: {len(pdata.symbols)} symbols, last bar {pdata.last_date.date()}, missing={list(pdata.missing)} (source {pdata.source})")
    except Exception as exc:
        print(f"data: FAILED — {exc}")
        return 3
    try:
        b = AlpacaBroker(live=(cfg.mode == "live"))
        a = b.get_account()
        clock = b.get_clock()
        print(f"alpaca: OK paper={a.is_paper} status={a.status} equity=${a.equity:,.2f} cash=${a.cash:,.2f} "
              f"market_open={clock.is_open} next_open={clock.next_open}")
        pos = b.get_positions()
        print(f"positions: {len(pos)} → " + ", ".join(f"{p.symbol} ${p.market_value:,.0f}" for p in pos))
        return 0
    except AuthError as exc:
        print(f"alpaca: AUTH FAILED — {exc}")
        return 3
    except LiveTradingBlocked as exc:
        print(f"alpaca: LIVE BLOCKED — {exc}")
        return 3
    except BrokerError as exc:
        print(f"alpaca: ERROR — {exc}")
        return 3


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--execute", action="store_true", help="place orders (default is dry-run)")
    ap.add_argument("--preflight", action="store_true", help="check config, data and broker; no plan")
    ap.add_argument("--fake", action="store_true", help="use an in-memory FakeBroker seeded with $100k (offline testing)")
    ap.add_argument("--ignore-market-hours", action="store_true")
    ap.add_argument("--recompute", action="store_true", help="force signal recomputation even if this period already has stored targets")
    ap.add_argument("--reset-kill-switch", action="store_true")
    ap.add_argument("--set-kill-switch", action="store_true")
    ap.add_argument("--reason", default="")
    ap.add_argument("--json", action="store_true", help="print full JSON summary")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        cfg = load_config(Path(args.config)) if args.config else load_config()
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 3

    if args.set_kill_switch:
        st.set_kill_switch(True, args.reason or "manual", "human")
        print("kill switch SET")
        return 0
    if args.reset_kill_switch:
        if not args.reason:
            print("--reason is required to reset the kill switch (it is written to the audit log)")
            return 3
        st.set_kill_switch(False, args.reason, "human")
        print("kill switch cleared")
        return 0
    if args.preflight:
        return preflight(cfg)

    if args.fake:
        fake_dir = st.STATE_DIR / "fake"
        fake_dir.mkdir(parents=True, exist_ok=True)
        for name in ("LOCK_FILE", "KILL_SWITCH_FILE", "LAST_RUN_FILE", "EQUITY_HISTORY_FILE", "TARGETS_FILE", "ACCOUNT_FILE"):
            setattr(st, name, fake_dir / getattr(st, name).name)
        st.AUDIT_FILE = fake_dir / "audit.jsonl"
        print(f"[fake] using isolated state in {fake_dir}")
        pdata = fetch_prices(cfg.all_symbols, start=cfg.data_start)
        broker = FakeBroker(cash=100_000.0, prices={s: float(pdata.close[s].dropna().iloc[-1]) for s in pdata.symbols})
    else:
        try:
            broker = AlpacaBroker(live=(cfg.mode == "live"))
        except (AuthError, LiveTradingBlocked) as exc:
            print(f"broker error: {exc}")
            return 3

    try:
        with st.run_lock():
            result = run(cfg, broker, execute=args.execute, require_market_open=not args.ignore_market_hours, force_recompute=args.recompute)
    except st.LockHeld as exc:
        print(f"lock held: {exc}")
        return 4

    summary = result.summary()
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(f"[{result.run_date}] mode={result.mode} data_last={result.data_last_date} equity=${summary['equity'] or 0:,.2f}")
        print("targets: " + ", ".join(f"{k} {v:.1%}" for k, v in summary["targets"].items()))
        for name, t in summary["per_sleeve"].items():
            print(f"  sleeve {name}: " + ", ".join(f"{k} {v:.0%}" for k, v in t.items()))
        if result.risk:
            print("risk: " + result.risk.summary)
        if result.plan:
            print(f"plan: {len(result.plan.trades)} trades, turnover {result.plan.turnover_pct:.1%}" + (f" BLOCKED: {result.plan.blocked_reason}" if result.plan.blocked_reason else ""))
            for t in result.plan.trades:
                print(f"  {t.side:4s} {t.symbol:6s} ${t.notional:>10,.2f} {'' if t.qty is None else f'qty {t.qty:g}'} ({t.reason})")
            for s, why in result.plan.skipped:
                print(f"  skip {s:6s} {why}")
        if result.execution:
            ex = result.execution
            print(f"execution: {'DRY-RUN' if ex.dry_run else 'LIVE-ON-BROKER'} filled={ex.n_filled} failed={list(ex.failed)} {ex.skipped_reason}")
        if result.reconcile_issues:
            print("reconcile issues: " + "; ".join(result.reconcile_issues))
        if result.notes:
            print("notes: " + " | ".join(result.notes))
        if result.error:
            print("ERROR: " + result.error)
    if result.error:
        return 3
    if result.mode == "halted" or (result.plan and result.plan.blocked_reason):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
