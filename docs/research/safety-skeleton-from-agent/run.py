"""Daily rebalance entrypoint skeleton for the safety layer (the ONLY thing a Claude session runs to trade).

    .venv/bin/python -m portfolio.safety.run --mode {dry-run,paper,live} [--date YYYY-MM-DD] [--root DIR]
                                             [--targets-json F] [--prices-json F] [--ignore-window]

Exit codes: 0 done or cleanly skipped, 1 failure, 2 halted (preflight/breaker), 3 auth (keys invalid),
4 lock held, 5 gate refused. Every phase transition is persisted; a re-run on the same date resumes, never re-plans.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .audit import AuditLog
from .breaker import RESET_FILE, BreakerState, Decision, evaluate
from .broker import AlpacaBroker, AuthError, Broker, FakeBroker
from .checks import (check_account, check_bars_fresh, check_is_trading_day, check_missing_symbols,
                     check_price_agreement, check_prices_finite, check_trading_window, last_session_on_or_before,
                     summarize)
from .config import Limits, load_limits
from .execute import await_fills, submit_plan
from .gate import GateRefused, Mode, assert_account_matches, resolve_mode
from .health import alert_conditions, write_heartbeat
from .ids import run_id
from .lock import RESUME_AT_RECONCILE, TRANSITIONS, AlreadyRunning, Phase, RunLock, RunState
from .rebalance import Plan, PlannedOrder, plan_orders
from .reconcile import cancel_stale_open_orders, compare, expected_after, positions_map, snapshot
from .secrets import MissingCredentials, load_alpaca_creds

ROOT = Path(__file__).resolve().parents[2]
ET = ZoneInfo("America/New_York")
EXIT_OK, EXIT_FAIL, EXIT_HALT, EXIT_AUTH, EXIT_LOCK, EXIT_GATE = 0, 1, 2, 3, 4, 5
log = logging.getLogger("portfolio.safety.run")


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def state(self) -> Path: return self.root / "state"
    @property
    def logs(self) -> Path: return self.root / "logs"
    @property
    def reports(self) -> Path: return self.root / "reports"
    @property
    def limits(self) -> Path: return self.root / "config" / "limits.yaml"
    @property
    def env_file(self) -> Path: return self.root / ".env"


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", required=True, choices=[m.value for m in Mode])
    p.add_argument("--date", type=date.fromisoformat, default=None, help="run date (default: today ET)")
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--targets-json", type=Path, default=None, help="{symbol: weight}; bypasses the signal module (replay)")
    p.add_argument("--prices-json", type=Path, default=None, help='{"prices": {sym: px}, "last_bar": "YYYY-MM-DD"}; bypasses yfinance')
    p.add_argument("--ignore-window", action="store_true", help="skip the 9:35-15:45 ET window check (dry-run/replay only)")
    return p.parse_args(argv)


# --- inputs ------------------------------------------------------------------------------------
def load_targets(args: argparse.Namespace, run_date: date) -> dict[str, float]:
    if args.targets_json:
        return {str(k): float(v) for k, v in json.loads(args.targets_json.read_text()).items()}
    try:
        from portfolio.signals.targets import compute_targets  # type: ignore  # strategy module (engine-owned)
    except ImportError as e:
        raise SystemExit(f"no signal module: {e}; pass --targets-json") from e
    return compute_targets(run_date)


def load_prices(args: argparse.Namespace, symbols: list[str]) -> tuple[dict[str, float], Optional[date]]:
    if args.prices_json:
        raw = json.loads(args.prices_json.read_text())
        return {k: float(v) for k, v in raw["prices"].items()}, date.fromisoformat(raw["last_bar"])
    import pandas as pd
    import yfinance as yf

    df = yf.download(symbols, period="10d", interval="1d", auto_adjust=True, progress=False)
    close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].rename(columns={"Close": symbols[0]})
    close = close.dropna(how="all")
    if close.empty:
        return {}, None
    prices = {s: float(close[s].dropna().iloc[-1]) for s in close.columns if close[s].notna().any()}
    return prices, close.index[-1].date()


def build_broker(mode: Mode, paths: Paths, prices: dict[str, float], audit: AuditLog) -> Broker:
    try:
        creds = load_alpaca_creds(paths.env_file)
    except MissingCredentials as e:
        if mode is not Mode.DRY_RUN:
            raise
        audit.write("broker.fake", level="warn", reason=f"no credentials ({e}); dry-run on local FakeBroker")
        return _fake_from_state(paths, prices)
    audit.write("broker.alpaca", paper=mode is not Mode.LIVE, key_fingerprint=creds.fingerprint(), source=creds.source)
    return AlpacaBroker(creds.key_id.reveal(), creds.secret_key.reveal(), paper=mode is not Mode.LIVE)


def _fake_from_state(paths: Paths, prices: dict[str, float]) -> FakeBroker:
    snap = paths.state / "positions_latest.json"
    positions = {p["symbol"]: float(p["qty"]) for p in json.loads(snap.read_text())} if snap.exists() else {}
    cash_file = paths.state / "dry_run_cash.json"
    cash = float(json.loads(cash_file.read_text())["cash"]) if cash_file.exists() else 100_000.0
    return FakeBroker(cash=cash, prices=prices, positions=positions)


# --- phases ------------------------------------------------------------------------------------
def preflight(mode: Mode, args: argparse.Namespace, broker: Broker, run_date: date, targets: dict[str, float],
              prices: dict[str, float], last_bar: Optional[date], limits: Limits, audit: AuditLog):
    acct = broker.get_account()
    assert_account_matches(mode, broker.base_url, acct.id, acct.account_number, Paths(args.root).state)
    sessions = broker.get_calendar(run_date - timedelta(days=14), run_date + timedelta(days=1))
    positions = broker.get_positions()
    broker_px = {p.symbol: p.current_price for p in positions if p.current_price > 0}
    results = [
        check_is_trading_day(sessions, run_date),
        check_account(acct),
        check_missing_symbols(list(targets), prices),
        check_prices_finite(prices),
        check_bars_fresh(last_bar, last_session_on_or_before(sessions, run_date)),
        check_price_agreement({s: prices[s] for s in broker_px if s in prices}, broker_px, limits.price_sanity_tol),
    ]
    if not args.ignore_window:
        results.append(check_trading_window(broker.get_clock(), datetime.now(ET)))
    ok, results = summarize(results)
    for r in results:
        audit.write("preflight.check", level="info" if r.ok else "warn", name=r.name, ok=r.ok, severity=r.severity, detail=r.detail)
    return ok, acct, positions, results


def run_breaker(paths: Paths, acct, run_date: date, limits: Limits, audit: AuditLog) -> Decision:
    bpath = paths.state / "breaker.json"
    before = BreakerState.load(bpath, acct.equity)
    after, decision = evaluate(before, acct.equity, acct.last_equity, run_date, limits, paths.state / RESET_FILE)
    after.save(bpath)
    audit.write("breaker.decision", level="warn" if decision.level or decision.halt_new_orders else "info",
                decision=asdict(decision))
    return decision


def make_plan(paths: Paths, run_date: date, targets, positions, prices, acct, limits: Limits,
              decision: Decision, audit: AuditLog) -> Plan:
    plan_path = paths.state / "plans" / f"{run_date.isoformat()}.json"
    if plan_path.exists():  # frozen plan from an earlier attempt today: never re-plan
        raw = json.loads(plan_path.read_text())
        plan = Plan(tuple(PlannedOrder(**o) for o in raw["orders"]), tuple(raw["skipped"]), raw["nav"], raw["turnover"], raw["gross_scale"])
        audit.write("plan.reloaded", hash=plan.hash, n_orders=len(plan.orders))
        return plan
    pos_map = {p.symbol: p for p in positions}
    if decision.flatten:
        plan = plan_orders({}, pos_map, prices, acct.equity, acct.cash, limits, allow_buys=False, ignore_turnover_cap=True)
        audit.write("plan.flatten", level="warn", reasons=decision.reasons)
    else:
        plan = plan_orders(targets, pos_map, prices, acct.equity, acct.cash, limits, gross_scale=decision.gross_scale,
                           allow_buys=decision.allow_buys, allow_sells=decision.allow_sells)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps({"orders": [asdict(o) for o in plan.orders], "skipped": plan.skipped, "nav": plan.nav,
                                     "turnover": plan.turnover, "gross_scale": plan.gross_scale, "hash": plan.hash}, indent=1))
    audit.write("plan.created", hash=plan.hash, n_orders=len(plan.orders), turnover=round(plan.turnover, 4),
                orders=[asdict(o) for o in plan.orders], skipped=plan.skipped)
    return plan


def trade(broker: Broker, plan: Plan, run_date: date, limits: Limits, state: RunState, state_path: Path,
          audit: AuditLog, positions_before) -> tuple[RunState, dict]:
    if state.phase not in RESUME_AT_RECONCILE:
        cancel_stale_open_orders(broker, run_date, audit)
        state = state.advance(Phase.SUBMITTING); state.save(state_path)
        res = submit_plan(broker, plan, run_date, limits.strategy_version, audit, dry_run=False)
        for c in res.coids + res.skipped_existing:
            state = state.with_submitted(c)
        state = state.advance(Phase.SUBMITTED, f"{len(res.submitted)} submitted, {len(res.rejected)} rejected"); state.save(state_path)
        rejected = len(res.rejected)
    else:
        audit.write("run.resumed_at_reconcile", level="warn", phase=state.phase.value, coids=state.submitted_coids)
        rejected = 0
        if state.phase is Phase.SUBMITTING:
            state = state.advance(Phase.SUBMITTED, "resumed"); state.save(state_path)
    fills = await_fills(broker, state.submitted_coids, audit, wait_s=limits.order_fill_wait_s)
    actual = positions_map(broker.get_positions())
    expected = expected_after(positions_map(positions_before), fills)
    mism = compare(expected, actual)
    for m in mism:
        audit.write("reconcile.mismatch", level="error", **asdict(m))
    audit.write("reconcile.done", mismatches=len(mism), fills=[asdict(f) for f in fills])
    state = state.advance(Phase.RECONCILED, f"{len(mism)} mismatches"); state.save(state_path)
    return state, {"rejected": rejected, "mismatches": len(mism), "partial_fills": sum(1 for f in fills if 0 < f.filled_qty < f.qty)}


# --- main --------------------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    paths = Paths(args.root)
    limits = load_limits(paths.limits)
    try:
        mode = resolve_mode(args.mode, paths.state)
    except GateRefused as e:
        log.error("gate: %s", e)
        return EXIT_GATE
    run_date = args.date or datetime.now(ET).date()
    try:
        with RunLock(paths.state / "run.lock", limits.lock_stale_hours):
            return _run(mode, args, paths, limits, run_date)
    except AlreadyRunning as e:
        log.error("%s", e)
        return EXIT_LOCK


def _run(mode: Mode, args: argparse.Namespace, paths: Paths, limits: Limits, run_date: date) -> int:
    state_path = RunState.path_for(paths.state, run_date.isoformat())
    state = RunState.load(state_path) or RunState(run_date.isoformat(), run_id(run_date, 1))
    if state.phase is Phase.DONE:
        log.info("run for %s already DONE; nothing to do", run_date)
        return EXIT_OK
    if state.phase is Phase.HALTED:
        log.error("run for %s is HALTED; human review required", run_date)
        return EXIT_HALT
    if state.phase is Phase.FAILED:
        state = RunState(state.run_date, run_id(run_date, state.attempt + 1), attempt=state.attempt + 1,
                         plan_hash=state.plan_hash, submitted_coids=state.submitted_coids, history=state.history)
    audit = AuditLog(paths.logs / f"audit-{run_date:%Y%m}.jsonl", state.run_id)
    audit.write("run.start", mode=mode.value, date=run_date.isoformat(), phase=state.phase.value, attempt=state.attempt)
    summary: dict = {}
    try:
        targets = load_targets(args, run_date)
        prices, last_bar = load_prices(args, sorted(targets))
        broker = build_broker(mode, paths, prices, audit)
        if state.phase in RESUME_AT_RECONCILE:
            acct, positions = broker.get_account(), broker.get_positions()
            plan = make_plan(paths, run_date, targets, positions, prices, acct, limits, run_breaker(paths, acct, run_date, limits, audit), audit)
        else:
            state = state.advance(Phase.PREFLIGHT) if state.phase is Phase.INIT else state
            state.save(state_path)
            ok, acct, positions, _ = preflight(mode, args, broker, run_date, targets, prices, last_bar, limits, audit)
            decision = run_breaker(paths, acct, run_date, limits, audit)
            if not ok or (decision.halt_new_orders and not decision.flatten):
                state = state.advance(Phase.HALTED if decision.halt_new_orders else Phase.DONE, "preflight failed or halted")
                state.save(state_path)
                audit.write("run.skipped", level="warn", preflight_ok=ok, breaker=decision.reasons)
                return EXIT_HALT if state.phase is Phase.HALTED else EXIT_OK
            plan = make_plan(paths, run_date, targets, positions, prices, acct, limits, decision, audit)
            state = state.with_plan(plan.hash).advance(Phase.PLANNED); state.save(state_path)
            if mode is Mode.DRY_RUN:
                submit_plan(broker, plan, run_date, limits.strategy_version, audit, dry_run=True)
                state = state.advance(Phase.DONE, "dry-run"); state.save(state_path)
                audit.write("run.done", mode="dry-run", n_orders=len(plan.orders))
                return EXIT_OK
        state, summary = trade(broker, plan, run_date, limits, state, state_path, audit, positions)
        snapshot(broker.get_positions(), paths.state / "positions_latest.json")
        state = state.advance(Phase.DONE if summary["mismatches"] == 0 else Phase.HALTED, json.dumps(summary)); state.save(state_path)
        audit.write("run.done", **summary)
        return EXIT_OK if state.phase is Phase.DONE else EXIT_HALT
    except AuthError as e:
        audit.write("run.auth_failed", level="error", msg=str(e))
        state.advance(Phase.FAILED, "auth").save(state_path)
        return EXIT_AUTH
    except GateRefused as e:
        audit.write("run.gate_refused", level="error", msg=str(e))
        state.advance(Phase.FAILED, "gate").save(state_path)
        return EXIT_GATE
    except Exception as e:  # noqa: BLE001 - last resort: persist FAILED so the next attempt resumes correctly
        log.exception("run failed")
        audit.write("run.failed", level="error", error=f"{type(e).__name__}: {e}")
        if Phase.FAILED in TRANSITIONS[state.phase]:
            state.advance(Phase.FAILED, str(e)[:200]).save(state_path)
        return EXIT_FAIL
    finally:
        write_heartbeat(paths.state / "heartbeat.json", run_date=run_date.isoformat(), phase=state.phase.value, mode=mode.value,
                        alerts=[asdict(a) for a in alert_conditions(last_run_phase=state.phase.value,
                                                                    **{k: v for k, v in summary.items() if k in ("rejected", "mismatches", "partial_fills")})])


if __name__ == "__main__":
    sys.exit(main())
