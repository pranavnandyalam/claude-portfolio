"""Reconciliation: before trading (cancel stale open orders from other runs, snapshot positions) and after
(expected vs actual positions, drift report). Orphan positions (not from this engine) are reported, never touched."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Mapping, Sequence

from .audit import AuditLog
from .broker import Broker, OrderSnap, PositionSnap
from .ids import COID_PREFIX


@dataclass(frozen=True)
class Mismatch:
    symbol: str
    expected: float
    actual: float
    kind: str  # "qty" | "unexpected_position" | "missing_position"


def cancel_stale_open_orders(broker: Broker, run_date: date, audit: AuditLog) -> tuple[str, ...]:
    today_tag = f"{COID_PREFIX}-{run_date:%Y%m%d}-"
    canceled = []
    for o in broker.get_orders(status="open"):
        if o.client_order_id.startswith(today_tag):
            continue  # this run's (or a resumed run's) own orders
        broker.cancel(o.id)
        audit.write("reconcile.canceled_stale_order", level="warn", coid=o.client_order_id, symbol=o.symbol, status=o.status)
        canceled.append(o.client_order_id)
    return tuple(canceled)


def positions_map(positions: Sequence[PositionSnap]) -> dict[str, float]:
    return {p.symbol: p.qty for p in positions}


def expected_after(before: Mapping[str, float], fills: Sequence[OrderSnap]) -> dict[str, float]:
    out = dict(before)
    for f in fills:
        sign = 1.0 if f.side == "buy" else -1.0
        out[f.symbol] = out.get(f.symbol, 0.0) + sign * f.filled_qty
    return {s: q for s, q in out.items() if abs(q) > 1e-9}


def compare(expected: Mapping[str, float], actual: Mapping[str, float], tol_shares: float = 0.0) -> tuple[Mismatch, ...]:
    out = []
    for s in sorted(set(expected) | set(actual)):
        e, a = expected.get(s, 0.0), actual.get(s, 0.0)
        if s not in expected:
            out.append(Mismatch(s, e, a, "unexpected_position"))
        elif s not in actual:
            out.append(Mismatch(s, e, a, "missing_position"))
        elif abs(e - a) > tol_shares:
            out.append(Mismatch(s, e, a, "qty"))
    return tuple(out)


def orphan_positions(actual: Mapping[str, float], universe: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(s for s in actual if s not in set(universe)))


def snapshot(positions: Sequence[PositionSnap], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(p) for p in positions], indent=1))
