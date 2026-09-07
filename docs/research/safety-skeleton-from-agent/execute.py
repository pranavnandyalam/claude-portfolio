"""Idempotent order submission and fill handling.
Invariants: (1) never POST without checking for an existing order with the same client_order_id or same
(run_date, symbol) prefix; (2) a TransientError on POST is ambiguous -> re-check by client_order_id before any
retry; (3) DuplicateClientOrderId == already submitted -> fetch and continue; (4) OrderRejected is final;
(5) unfilled remainder at the deadline is canceled and rolled into tomorrow's plan; never chase, never replace."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Optional, Sequence

from .audit import AuditLog
from .broker import (TERMINAL, Broker, DuplicateClientOrderId, OrderRejected, OrderReq, OrderSnap, TransientError)
from .ids import client_order_id, coid_prefix
from .rebalance import Plan

BACKOFF_S: tuple[float, ...] = (1, 2, 4, 8)


@dataclass(frozen=True)
class SubmitResult:
    submitted: tuple[OrderSnap, ...]
    skipped_existing: tuple[str, ...]
    rejected: tuple[str, ...]
    dry_run: tuple[str, ...]

    @property
    def coids(self) -> tuple[str, ...]:
        return tuple(o.client_order_id for o in self.submitted)


def _submit_with_retry(broker: Broker, req: OrderReq, sleep: Callable[[float], None], rng: random.Random) -> OrderSnap:
    last: Optional[Exception] = None
    for i, wait in enumerate((0.0,) + BACKOFF_S):
        if i > 0:
            sleep(wait + rng.random() * 0.5)
            found = broker.get_order_by_client_id(req.client_order_id)  # ambiguous prior attempt?
            if found is not None:
                return found
        try:
            return broker.submit(req)
        except DuplicateClientOrderId:
            found = broker.get_order_by_client_id(req.client_order_id)
            if found is not None:
                return found
            raise
        except TransientError as e:
            last = e
    assert last is not None
    raise last


def submit_plan(broker: Broker, plan: Plan, run_date: date, strategy_version: str, audit: AuditLog, *,
                dry_run: bool, sleep: Callable[[float], None] = time.sleep,
                rng: Optional[random.Random] = None) -> SubmitResult:
    rng = rng or random.Random(0)
    submitted, skipped, rejected, dry = [], [], [], []
    day_start = datetime.combine(run_date, datetime.min.time(), tzinfo=timezone.utc)
    todays = () if dry_run else tuple(broker.get_orders(status="all", after=day_start))
    for o in plan.orders:
        coid = client_order_id(run_date, o.symbol, o.side, o.qty, strategy_version)
        req = OrderReq(o.symbol, o.side, o.qty, coid)
        if dry_run:
            audit.write("order.dry_run", symbol=o.symbol, side=o.side, qty=o.qty, coid=coid, reason=o.reason)
            dry.append(coid)
            continue
        prefix = coid_prefix(run_date, o.symbol)
        prior = next((t for t in todays if t.client_order_id.startswith(prefix)), None) or broker.get_order_by_client_id(coid)
        if prior is not None:
            audit.write("order.skip_existing", symbol=o.symbol, coid=prior.client_order_id, status=prior.status)
            skipped.append(prior.client_order_id)
            continue
        try:
            snap = _submit_with_retry(broker, req, sleep, rng)
        except OrderRejected as e:
            audit.write("order.rejected", level="warn", symbol=o.symbol, coid=coid, status=e.status, code=e.code, msg=str(e))
            rejected.append(coid)
            continue
        audit.write("order.submitted", symbol=o.symbol, side=o.side, qty=o.qty, coid=coid, broker_id=snap.id, status=snap.status)
        submitted.append(snap)
    return SubmitResult(tuple(submitted), tuple(skipped), tuple(rejected), tuple(dry))


def await_fills(broker: Broker, coids: Sequence[str], audit: AuditLog, *, wait_s: float, poll_s: float = 5.0,
                sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> tuple[OrderSnap, ...]:
    deadline = clock() + wait_s
    wanted = set(coids)
    while True:  # one GET /orders?status=open per poll (rate budget: 200 req/min), not one GET per order
        still_open = {o.client_order_id for o in broker.get_orders(status="open")}
        if not (wanted & still_open) or clock() >= deadline:
            break
        sleep(poll_s)
    out: list[OrderSnap] = []
    for coid in coids:
        s = broker.get_order_by_client_id(coid)
        if s is None:
            audit.write("order.vanished", level="error", coid=coid)
            continue
        if s.status not in TERMINAL:
            broker.cancel(s.id)
            s = broker.get_order_by_client_id(s.client_order_id) or s
            audit.write("order.canceled_at_deadline", level="warn", coid=s.client_order_id, filled_qty=s.filled_qty, qty=s.qty)
        if 0 < s.filled_qty < s.qty:
            audit.write("order.partial_fill", level="warn", coid=s.client_order_id, filled_qty=s.filled_qty, qty=s.qty)
        out.append(s)
    return tuple(out)
