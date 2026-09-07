"""The deterministic daily engine: data → sleeves → combined targets → vol targeting → risk →
order plan → (dry-run | execute) → state. This is the ONLY code path that places orders.

A supervising Claude session calls `scripts/run_daily.py`; it never computes targets itself.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import pandas as pd

from portfolio.broker import Account, Broker, BrokerError, Position, make_client_order_id
from portfolio.config import PortfolioConfig, Sleeve
from portfolio.data import DataError, PriceData, assert_fresh, fetch_prices, sanity_check_returns
from portfolio.executor import ExecutionResult, execute_plan, reconcile
from portfolio.rebalance import OrderPlan, build_order_plan
from portfolio.risk import RiskVerdict, evaluate, peak_equity
from portfolio.signals.core import portfolio_realized_vol, vol_target_scalar
from portfolio.signals.dual_momentum import GEMParams, gem_targets
from portfolio.signals.rotation import RotationParams, rotation_targets
from portfolio.signals.trend import TrendParams, trend_targets
from portfolio.state import (audit, check_account_pin, mark_targets_executed, read_equity_history, read_kill_switch,
                             read_targets, set_kill_switch, write_last_run, write_targets)

PRICE_AGREEMENT_TOL = 0.15   # broker latest price vs last close; larger = data problem

log = logging.getLogger(__name__)


def _tupled(params: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    out = dict(params)
    for k in keys:
        if k in out and isinstance(out[k], list):
            out[k] = tuple(out[k])
    return out


def period_key(date: pd.Timestamp, freq: str) -> str:
    """Rebalance period identifier: 'M' → YYYY-MM, 'W' → ISO year-week, 'D' → date."""
    d = pd.Timestamp(date)
    if freq == "M":
        return d.strftime("%Y-%m")
    if freq == "W":
        iso = d.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return d.strftime("%Y-%m-%d")


def sleeve_targets(sleeve: Sleeve, close: pd.DataFrame) -> dict[str, float]:
    """Dispatch to the sleeve implementation. Returns weights summing to <= 1 within the sleeve."""
    p = sleeve.params
    if sleeve.type == "trend":
        return trend_targets(close, TrendParams(**_tupled(p, ("assets",))))
    if sleeve.type == "gem":
        return gem_targets(close, GEMParams(**_tupled(p, ("horizons",))))
    if sleeve.type == "rotation":
        return rotation_targets(close, RotationParams(**_tupled(p, ("universe", "horizons"))))
    if sleeve.type == "static":
        return {str(k).upper(): float(v) for k, v in (p.get("weights") or {}).items()}
    raise ValueError(f"unsupported sleeve type {sleeve.type}")


def combine_sleeves(cfg: PortfolioConfig, close: pd.DataFrame) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Allocation-weighted sum of sleeve targets. Returns (combined, per_sleeve)."""
    combined: dict[str, float] = {}
    per: dict[str, dict[str, float]] = {}
    for s in cfg.enabled_sleeves:
        t = sleeve_targets(s, close)
        per[s.name] = t
        for sym, w in t.items():
            combined[sym] = combined.get(sym, 0.0) + s.allocation * w
    return combined, per


def apply_vol_target(cfg: PortfolioConfig, targets: dict[str, float], close: pd.DataFrame) -> tuple[dict[str, float], float, float]:
    """Scale risky exposure to hit cfg.vol_target (cap 1.0, i.e. de-risk only). Returns
    (targets, realized_vol, scalar)."""
    if not cfg.vol_target:
        return targets, float("nan"), 1.0
    risky = {s: w for s, w in targets.items() if s != cfg.cash_symbol}
    rv = portfolio_realized_vol(close, risky, cfg.vol_window)
    k = vol_target_scalar(rv, cfg.vol_target, cap=1.0)
    scaled = {s: w * k for s, w in risky.items()}
    cash = max(0.0, 1.0 - sum(scaled.values()))
    if cash > 1e-9:
        scaled[cfg.cash_symbol] = cash
    return scaled, rv, k


@dataclass(frozen=True)
class RunResult:
    run_date: str
    mode: str                     # dry-run | execute | halted | error
    targets: dict[str, float]
    per_sleeve: dict[str, dict[str, float]]
    risk: RiskVerdict | None
    plan: OrderPlan | None
    execution: ExecutionResult | None
    account: Account | None
    positions: tuple[Position, ...]
    data_last_date: str
    realized_vol: float
    vol_scalar: float
    reconcile_issues: tuple[str, ...] = field(default_factory=tuple)
    error: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> dict[str, Any]:
        return {
            "run_date": self.run_date, "mode": self.mode, "error": self.error, "data_last_date": self.data_last_date,
            "equity": self.account.equity if self.account else None, "cash": self.account.cash if self.account else None,
            "targets": {k: round(v, 4) for k, v in sorted(self.targets.items())},
            "per_sleeve": {n: {k: round(v, 4) for k, v in t.items()} for n, t in self.per_sleeve.items()},
            "risk": self.risk.summary if self.risk else None,
            "risk_halts": list(self.risk.halts) if self.risk else [],
            "risk_warnings": list(self.risk.warnings) if self.risk else [],
            "drawdown_pct": self.risk.drawdown_pct if self.risk else None,
            "realized_vol": None if pd.isna(self.realized_vol) else round(self.realized_vol, 4),
            "vol_scalar": round(self.vol_scalar, 3),
            "plan": None if self.plan is None else {
                "trades": [{"symbol": t.symbol, "side": t.side, "notional": round(t.notional, 2), "qty": t.qty, "reason": t.reason} for t in self.plan.trades],
                "skipped": list(self.plan.skipped), "turnover_pct": round(self.plan.turnover_pct, 4), "blocked_reason": self.plan.blocked_reason, "notes": list(self.plan.notes),
            },
            "execution": None if self.execution is None else {
                "dry_run": self.execution.dry_run, "skipped_reason": self.execution.skipped_reason, "n_filled": self.execution.n_filled,
                "failed": list(self.execution.failed),
                "orders": [{"symbol": o.symbol, "side": o.side, "status": o.status, "filled_qty": o.filled_qty, "price": o.filled_avg_price} for o in self.execution.submitted],
            },
            "reconcile_issues": list(self.reconcile_issues),
            "positions": [{"symbol": p.symbol, "qty": p.qty, "market_value": round(p.market_value, 2)} for p in self.positions],
            "notes": list(self.notes),
        }


def run(
    cfg: PortfolioConfig,
    broker: Broker,
    execute: bool = False,
    today: pd.Timestamp | None = None,
    price_loader: Callable[[list[str], str], PriceData] | None = None,
    require_market_open: bool = True,
    sleep=None,
    force_recompute: bool = False,
    now_et=None,
) -> RunResult:
    """One full engine pass. Never raises for expected failures; returns RunResult with mode/error."""
    today = pd.Timestamp(today or pd.Timestamp.now(tz="America/New_York").tz_localize(None)).normalize()
    run_date = today.strftime("%Y%m%d")
    loader = price_loader or (lambda syms, start: fetch_prices(syms, start=start))
    notes: list[str] = []
    audit("engine_start", run_date=run_date, execute=execute, mode=cfg.mode)

    # 1. Data
    try:
        pdata = loader(cfg.all_symbols, cfg.data_start)
        close = pdata.close.ffill()
        last = assert_fresh(pdata.close, cfg.risk.max_stale_bdays, today)
    except DataError as exc:
        audit("engine_error", stage="data", error=str(exc))
        return RunResult(run_date, "error", {}, {}, None, None, None, None, (), "", float("nan"), 1.0, error=f"data: {exc}")
    data_issues: list[str] = []
    if pdata.missing:
        notes.append(f"missing symbols: {', '.join(pdata.missing)}")
        if cfg.cash_symbol in pdata.missing:
            data_issues.append(f"cash symbol {cfg.cash_symbol} has no data")
    outliers = sanity_check_returns(pdata.close, cfg.risk.max_abs_daily_return)
    if outliers:
        data_issues.append(f"suspicious last-day moves in {', '.join(outliers)}")

    # 2. Targets — recomputed once per rebalance period (matches the monthly backtest); other
    #    days reuse the stored targets so daily signal noise does not churn the book.
    pk = period_key(today, cfg.schedule.rebalance_freq)
    stored = read_targets()
    needs_recompute = force_recompute or stored.get("period_key") != pk or not stored.get("targets")
    # "Rebalance day" = this period's targets have not been executed yet (covers: dry-run then execute
    # on the same day, a skipped/halted first attempt, or the app being closed on day 1).
    is_rebalance_day = needs_recompute or not stored.get("executed_on")
    try:
        if needs_recompute:
            combined, per = combine_sleeves(cfg, close)
            targets, rv, k = apply_vol_target(cfg, combined, close)
            write_targets(str(last.date()), pk, targets, per, {"realized_vol": None if pd.isna(rv) else rv, "vol_scalar": k, "computed_on": run_date})
            audit("targets_recomputed", period_key=pk, targets=targets)
        else:
            targets = {str(s): float(w) for s, w in stored["targets"].items()}
            per = {n: {str(s): float(w) for s, w in t.items()} for n, t in (stored.get("per_sleeve") or {}).items()}
            rv = float(stored.get("realized_vol") or float("nan"))
            k = float(stored.get("vol_scalar") or 1.0)
            notes.append(f"using stored targets from {stored.get('as_of')} (period {pk}); next recompute at period change")
    except Exception as exc:  # a sleeve bug must not crash into a half-state
        audit("engine_error", stage="signals", error=repr(exc))
        return RunResult(run_date, "error", {}, {}, None, None, None, None, (), str(last.date()), float("nan"), 1.0, error=f"signals: {exc!r}")

    # 3. Account + risk
    try:
        account = broker.get_account()
        positions = broker.get_positions()
    except BrokerError as exc:
        audit("engine_error", stage="broker", error=str(exc))
        return RunResult(run_date, "error", targets, per, None, None, None, None, (), str(last.date()), rv, k, error=f"broker: {exc}")
    if cfg.mode == "paper" and not account.is_paper:
        audit("engine_error", stage="gate", error="config mode is paper but broker is LIVE")
        return RunResult(run_date, "error", targets, per, None, None, None, account, tuple(positions), str(last.date()), rv, k, error="gate: config paper but broker live")
    pin_problem = "" if account.account_id == "fake" else check_account_pin(account.account_id, account.is_paper)
    if pin_problem:
        audit("engine_error", stage="gate", error=pin_problem)
        return RunResult(run_date, "error", targets, per, None, None, None, account, tuple(positions), str(last.date()), rv, k, error=f"gate: {pin_problem}")
    if not account.can_trade:
        data_issues.append(f"account cannot trade (status={account.status}, blocked={account.trading_blocked})")
    # Price agreement: broker quotes vs our last close for the symbols we intend to hold.
    if hasattr(broker, "latest_price"):
        disagreements = []
        for sym in sorted(targets):
            if sym not in close.columns or pd.isna(close[sym].iloc[-1]):
                continue
            try:
                bp = float(broker.latest_price(sym))
            except BrokerError:
                continue
            ref = float(close[sym].iloc[-1])
            if ref > 0 and abs(bp / ref - 1.0) > PRICE_AGREEMENT_TOL:
                disagreements.append(f"{sym} broker {bp:.2f} vs close {ref:.2f}")
        if disagreements:
            data_issues.append("price disagreement: " + "; ".join(disagreements))

    peak = peak_equity(read_equity_history(), account.equity)
    ks = read_kill_switch()
    verdict = evaluate(targets, account, peak, cfg.risk, kill_switch_active=ks.active, data_issues=data_issues, risk_off_symbol=cfg.cash_symbol)
    audit("risk_verdict", ok=verdict.ok, halts=list(verdict.halts), warnings=list(verdict.warnings), drawdown=verdict.drawdown_pct, daily=verdict.daily_pnl_pct)

    if verdict.circuit_breaker and not ks.active:
        set_kill_switch(True, f"circuit breaker: drawdown {verdict.drawdown_pct:.1%}", "engine")
        notes.append("kill switch SET by circuit breaker; requires human reset (scripts/run_daily.py --reset-kill-switch)")

    # 4. Plan
    prices: dict[str, float] = {s: float(close[s].iloc[-1]) for s in close.columns if pd.notna(close[s].iloc[-1])}
    for p in positions:  # positions not in our data universe still need a price for exits
        prices.setdefault(p.symbol, p.current_price)
    plan_targets = verdict.targets
    frac = None
    if hasattr(broker, "is_fractionable"):
        cache: dict[str, bool] = {}

        def frac(sym: str) -> bool:  # noqa: E306
            if sym not in cache:
                try:
                    cache[sym] = broker.is_fractionable(sym)
                except BrokerError:
                    cache[sym] = False
            return cache[sym]

    from dataclasses import replace as _replace
    rb_params = cfg.rebalance if is_rebalance_day else _replace(cfg.rebalance, drift_band_abs=cfg.rebalance.mid_period_drift_band)
    plan = build_order_plan(plan_targets, positions, account, prices, run_date, rb_params, fractionable=frac,
                            client_id_fn=lambda s, side: make_client_order_id(run_date, s, side))
    # Turnover bug-catcher: large turnover is expected on rebalance days / circuit breaker, not otherwise.
    if not is_rebalance_day and not verdict.circuit_breaker and plan.turnover_pct > cfg.risk.max_turnover_pct:
        plan = _replace(plan, blocked_reason=f"mid-period turnover {plan.turnover_pct:.1%} exceeds risk cap {cfg.risk.max_turnover_pct:.0%}")
    audit("order_plan", run_date=run_date, n_trades=len(plan.trades), turnover_pct=plan.turnover_pct, blocked=plan.blocked_reason,
          trades=[{"symbol": t.symbol, "side": t.side, "notional": t.notional, "qty": t.qty} for t in plan.trades])

    # 5. Execute (or not)
    halted = (not verdict.ok) and not verdict.circuit_breaker
    if halted:
        mode = "halted"
        execution = ExecutionResult(skipped_reason="; ".join(verdict.halts), dry_run=not execute)
    else:
        kwargs = {"sleep": sleep} if sleep is not None else {}
        execution = execute_plan(broker, plan, dry_run=not execute, require_market_open=require_market_open, now_et=now_et, **kwargs)
        mode = "execute" if execute else "dry-run"
    issues: tuple[str, ...] = ()
    if execute and not execution.dry_run and not execution.skipped_reason:
        if is_rebalance_day and not plan.blocked_reason and not execution.failed:
            mark_targets_executed(run_date)
        try:
            issues = tuple(reconcile(broker, plan_targets, tolerance=max(cfg.rebalance.drift_band_abs, 0.03) + 0.01))
            positions = broker.get_positions()
            account = broker.get_account()
        except BrokerError as exc:
            issues = (f"reconcile failed: {exc}",)

    notes.append(f"rebalance_day={is_rebalance_day} period={pk}")
    result = RunResult(run_date, mode, targets, per, verdict, plan, execution, account, tuple(positions), str(last.date()), rv, k, issues, "", tuple(notes))
    write_last_run(result.summary())
    audit("engine_end", run_date=run_date, mode=mode, n_orders=len(execution.submitted), failed=list(execution.failed), skipped=execution.skipped_reason)
    return result
