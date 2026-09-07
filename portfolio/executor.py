"""Apply an OrderPlan to a Broker safely.

Sequence: preflight (paper assertion, market open, no stale open orders) → sells → wait for
fills → buys → wait → reconcile. Every submission is audited before and after. Failures on one
symbol never abort the others; the result lists them.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from portfolio.broker import Broker, BrokerError, Order, OrderRequest
from portfolio.rebalance import OrderPlan, PlannedTrade
from portfolio.state import audit

FILL_POLL_SECONDS = 2.0
FILL_TIMEOUT_SECONDS = 90.0
WINDOW_START = (9, 35)    # ET; avoid the opening auction
WINDOW_END = (15, 45)     # ET; leave time for fills before the close


def in_trading_window(now_et=None, start: tuple[int, int] = WINDOW_START, end: tuple[int, int] = WINDOW_END) -> bool:
    import pandas as pd

    now = now_et or pd.Timestamp.now(tz="America/New_York")
    t = (now.hour, now.minute)
    return start <= t <= end


@dataclass(frozen=True)
class ExecutionResult:
    submitted: tuple[Order, ...] = field(default_factory=tuple)
    failed: tuple[tuple[str, str], ...] = field(default_factory=tuple)   # (symbol, reason)
    skipped_reason: str = ""                                             # whole-plan skip
    dry_run: bool = False

    @property
    def n_filled(self) -> int:
        return sum(1 for o in self.submitted if o.status == "filled")

    @property
    def ok(self) -> bool:
        return not self.failed and not self.skipped_reason


def _to_request(t: PlannedTrade) -> OrderRequest:
    if t.qty is not None:
        return OrderRequest(symbol=t.symbol, side=t.side, qty=t.qty, client_order_id=t.client_order_id, reason=t.reason)
    return OrderRequest(symbol=t.symbol, side=t.side, notional=round(t.notional, 2), client_order_id=t.client_order_id, reason=t.reason)


def _wait_for_terminal(broker: Broker, orders: list[Order], timeout: float, poll: float, sleep=time.sleep) -> list[Order]:
    deadline = time.monotonic() + timeout
    latest = {o.id: o for o in orders}
    while time.monotonic() < deadline and any(not o.is_terminal for o in latest.values()):
        sleep(poll)
        for oid, o in list(latest.items()):
            if o.is_terminal:
                continue
            try:
                latest[oid] = broker.get_order(oid)
            except BrokerError as exc:
                audit("order_poll_error", order_id=oid, error=str(exc))
    return list(latest.values())


def execute_plan(
    broker: Broker,
    plan: OrderPlan,
    dry_run: bool = True,
    require_market_open: bool = True,
    fill_timeout: float = FILL_TIMEOUT_SECONDS,
    poll: float = FILL_POLL_SECONDS,
    sleep=time.sleep,
    now_et=None,
) -> ExecutionResult:
    if plan.blocked_reason:
        audit("execution_skipped", reason=plan.blocked_reason, run_date=plan.run_date)
        return ExecutionResult(skipped_reason=plan.blocked_reason, dry_run=dry_run)
    if plan.is_empty:
        audit("execution_skipped", reason="empty plan", run_date=plan.run_date)
        return ExecutionResult(skipped_reason="empty plan", dry_run=dry_run)
    if dry_run:
        audit("dry_run_plan", run_date=plan.run_date, trades=[t.__dict__ for t in plan.trades], turnover_usd=plan.turnover_usd)
        return ExecutionResult(dry_run=True)

    if not getattr(broker, "is_paper", False):
        # Live is allowed only through the explicit gate in broker.AlpacaBroker; log loudly.
        audit("LIVE_EXECUTION", run_date=plan.run_date, n_trades=len(plan.trades))
    if require_market_open:
        try:
            clock = broker.get_clock()
        except BrokerError as exc:
            audit("execution_skipped", reason=f"clock error: {exc}")
            return ExecutionResult(skipped_reason=f"clock error: {exc}")
        if not clock.is_open:
            audit("execution_skipped", reason="market closed", next_open=str(clock.next_open))
            return ExecutionResult(skipped_reason=f"market closed (next open {clock.next_open})")
        if not in_trading_window(now_et):
            audit("execution_skipped", reason="outside trading window")
            return ExecutionResult(skipped_reason=f"outside execution window {WINDOW_START[0]:02d}:{WINDOW_START[1]:02d}-{WINDOW_END[0]:02d}:{WINDOW_END[1]:02d} ET")

    try:
        open_orders = broker.list_open_orders()
    except BrokerError as exc:
        return ExecutionResult(skipped_reason=f"could not list open orders: {exc}")
    if open_orders:
        audit("cancel_open_orders", n=len(open_orders), ids=[o.id for o in open_orders])
        try:
            broker.cancel_all_orders()
            sleep(poll)
        except BrokerError as exc:
            return ExecutionResult(skipped_reason=f"could not cancel stale open orders: {exc}")

    submitted: list[Order] = []
    failed: list[tuple[str, str]] = []

    def _submit(trades: list[PlannedTrade]) -> list[Order]:
        out: list[Order] = []
        for t in trades:
            try:
                req = _to_request(t)
            except ValueError as exc:
                failed.append((t.symbol, f"invalid request: {exc}"))
                continue
            audit("order_submit", symbol=t.symbol, side=t.side, notional=t.notional, qty=t.qty,
                  client_order_id=t.client_order_id, reason=t.reason, run_date=plan.run_date)
            try:
                o = broker.submit_order(req)
            except BrokerError as exc:
                failed.append((t.symbol, str(exc)))
                audit("order_failed", symbol=t.symbol, side=t.side, error=str(exc))
                continue
            audit("order_accepted", symbol=t.symbol, order_id=o.id, status=o.status, already_existed=o.already_existed)
            out.append(o)
        return out

    sells = _submit(plan.sells)
    sells = _wait_for_terminal(broker, sells, fill_timeout, poll, sleep) if sells else []
    submitted.extend(sells)
    unfilled_sells = [o for o in sells if o.status != "filled"]
    if unfilled_sells:
        audit("sells_not_filled", ids=[o.id for o in unfilled_sells])
        # Buys depend on sell proceeds; proceed anyway (buys may fail on cash) but flag it.
        for o in unfilled_sells:
            failed.append((o.symbol, f"sell not filled (status {o.status})"))

    buys = _submit(plan.buys)
    buys = _wait_for_terminal(broker, buys, fill_timeout, poll, sleep) if buys else []
    submitted.extend(buys)
    for o in buys:
        if o.status != "filled":
            failed.append((o.symbol, f"buy not filled (status {o.status})"))

    for o in submitted:
        audit("order_final", symbol=o.symbol, order_id=o.id, status=o.status, filled_qty=o.filled_qty, filled_avg_price=o.filled_avg_price)
    return ExecutionResult(submitted=tuple(submitted), failed=tuple(failed), dry_run=False)


def reconcile(broker: Broker, targets: dict[str, float], tolerance: float = 0.03) -> list[str]:
    """Compare live weights vs targets after execution. Returns human-readable discrepancies."""
    account = broker.get_account()
    positions = {p.symbol: p.market_value / account.equity for p in broker.get_positions()} if account.equity else {}
    issues: list[str] = []
    for sym in sorted(set(targets) | set(positions)):
        tw, cw = targets.get(sym, 0.0), positions.get(sym, 0.0)
        if abs(tw - cw) > tolerance:
            issues.append(f"{sym}: target {tw:.1%} vs actual {cw:.1%}")
    return issues
