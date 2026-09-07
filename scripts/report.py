#!/usr/bin/env python
"""Post-close report: account snapshot, equity history, performance vs benchmark, last run,
audit highlights. Writes reports/YYYY-MM-DD.md and appends state/equity_history.csv.
    .venv/bin/python scripts/report.py [--fake] [--no-write]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from portfolio import state as st
from portfolio.broker import AlpacaBroker, AuthError, BrokerError, FakeBroker
from portfolio.config import load_config
from portfolio.data import fetch_prices
from portfolio.stats import compute_stats, drawdown_series

ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "reports"


def build_report(cfg, broker, today: str) -> str:
    account = broker.get_account()
    positions = broker.get_positions()
    st.append_equity(today, account.equity, account.cash)
    hist = st.read_equity_history()
    lines = [f"# Portfolio report — {today}", "",
             f"- Mode: **{cfg.mode}** (broker paper={account.is_paper})",
             f"- Equity: **${account.equity:,.2f}**  cash ${account.cash:,.2f}  day {account.equity / account.last_equity - 1:+.2%}" if account.last_equity else f"- Equity: ${account.equity:,.2f}",
             f"- Kill switch: {'ACTIVE — ' + st.read_kill_switch().reason if st.read_kill_switch().active else 'off'}", ""]
    lines.append("## Positions")
    if positions:
        lines.append("| symbol | qty | value | weight | unrealized |")
        lines.append("|---|---:|---:|---:|---:|")
        for p in sorted(positions, key=lambda x: -x.market_value):
            lines.append(f"| {p.symbol} | {p.qty:g} | ${p.market_value:,.0f} | {p.market_value / account.equity:.1%} | ${p.unrealized_pl:,.0f} |")
    else:
        lines.append("(none)")
    lines.append("")
    if len(hist) >= 5:
        eq = pd.Series({pd.Timestamp(d): e for d, e, _ in hist}).sort_index()
        try:
            s = compute_stats(eq)
            dd = drawdown_series(eq).iloc[-1]
            bench = fetch_prices([cfg.benchmark], start=str(eq.index[0].date())).close[cfg.benchmark].reindex(eq.index).ffill()
            b = compute_stats(bench.dropna()) if bench.notna().sum() >= 5 else None
            lines += ["## Performance (since tracking began)",
                      f"- Since {s.start}: total {s.total_return:+.2%}, CAGR {s.cagr:+.2%}, vol {s.ann_vol:.1%}, Sharpe {s.sharpe:.2f}, max DD {s.max_drawdown:.1%}, current DD {dd:.1%}"]
            if b:
                lines.append(f"- {cfg.benchmark} same window: total {b.total_return:+.2%}, vol {b.ann_vol:.1%}, Sharpe {b.sharpe:.2f}, max DD {b.max_drawdown:.1%}")
            lines.append("")
        except Exception as exc:  # stats must never break the report
            lines += [f"(performance stats unavailable: {exc})", ""]
    last = st.read_last_run()
    if last:
        lines += ["## Last engine run", f"- {last.get('written_at')} mode={last.get('mode')} data_last={last.get('data_last_date')} error={last.get('error') or 'none'}",
                  "- targets: " + ", ".join(f"{k} {v:.1%}" for k, v in (last.get("targets") or {}).items()),
                  f"- risk: {last.get('risk')}"]
        ex = last.get("execution") or {}
        if ex:
            lines.append(f"- execution: dry_run={ex.get('dry_run')} filled={ex.get('n_filled')} failed={ex.get('failed')} {ex.get('skipped_reason') or ''}")
        if last.get("reconcile_issues"):
            lines.append("- reconcile issues: " + "; ".join(last["reconcile_issues"]))
        lines.append("")
    recent = st.read_audit(last_n=200)
    today_events = [r for r in recent if r.get("ts", "").startswith(today)]
    orders = [r for r in today_events if r.get("event") in ("order_final", "order_failed")]
    lines += ["## Today's audit", f"- events: {len(today_events)}, orders finalized/failed: {len(orders)}"]
    for r in orders[-20:]:
        lines.append(f"  - {r.get('event')} {r.get('symbol')} {r.get('status', '')} qty={r.get('filled_qty', '')} px={r.get('filled_avg_price', '')} {r.get('error', '')}")
    halts = [r for r in today_events if r.get("event") == "risk_verdict" and r.get("halts")]
    if halts:
        lines.append(f"- HALTS today: {halts[-1]['halts']}")
    lines += ["", "_Paper account. Systematic rules; not investment advice._"]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default today, America/New_York)")
    args = ap.parse_args()
    cfg = load_config(Path(args.config)) if args.config else load_config()
    today = args.date or pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d")
    if args.fake:
        pdata = fetch_prices(cfg.all_symbols, start=cfg.data_start)
        broker = FakeBroker(cash=100_000.0, prices={s: float(pdata.close[s].dropna().iloc[-1]) for s in pdata.symbols})
    else:
        try:
            broker = AlpacaBroker(live=(cfg.mode == "live"))
        except AuthError as exc:
            print(f"broker auth error: {exc}")
            return 3
    try:
        text = build_report(cfg, broker, today)
    except BrokerError as exc:
        print(f"broker error: {exc}")
        return 3
    print(text)
    if not args.no_write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / f"{today}.md").write_text(text)
        st.audit("report_written", date=today, path=str(REPORT_DIR / f"{today}.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
