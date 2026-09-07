"""Target weights -> whole-share order plan. Pure, deterministic, side-effect free.
Applies: weight clipping, drift bands (no churn), min/max order notional, sells-first cash budgeting,
turnover cap (sells prioritized), order-count cap, breaker restrictions (allow_buys/allow_sells/gross_scale)."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from typing import Mapping, Optional

from .broker import PositionSnap
from .config import Limits

SELL_PROCEEDS_HAIRCUT = 0.95


@dataclass(frozen=True)
class PlannedOrder:
    symbol: str
    side: str
    qty: int
    est_price: float
    reason: str

    @property
    def notional(self) -> float:
        return self.qty * self.est_price


@dataclass(frozen=True)
class Plan:
    orders: tuple[PlannedOrder, ...]
    skipped: tuple[str, ...]
    nav: float
    turnover: float
    gross_scale: float

    @property
    def hash(self) -> str:
        body = json.dumps([asdict(o) for o in self.orders], sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()[:12]


def _clip_targets(targets: Mapping[str, float], limits: Limits, gross_scale: float, skipped: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for sym, w in targets.items():
        if w < 0:
            skipped.append(f"{sym}: negative weight (long-only)")
            continue
        if w > limits.max_position_weight:
            skipped.append(f"{sym}: weight {w:.3f} clipped to {limits.max_position_weight}")
        out[sym] = min(w, limits.max_position_weight) * gross_scale
    gross = sum(out.values())
    if gross > limits.max_gross_exposure:
        out = {s: w * limits.max_gross_exposure / gross for s, w in out.items()}
    return out


def _candidate(sym: str, tgt_w: float, pos: Optional[PositionSnap], price: float, nav: float,
               limits: Limits, skipped: list[str]) -> Optional[PlannedOrder]:
    cur_qty = pos.qty if pos else 0.0
    cur_w = cur_qty * price / nav
    diff_w = tgt_w - cur_w
    exiting = tgt_w == 0 and cur_qty > 0
    band = max(limits.drift_band_abs, limits.drift_band_rel * tgt_w)
    if not exiting and abs(diff_w) < band:
        return None
    if diff_w > 0:
        side, qty, reason = "buy", math.floor(diff_w * nav / price), f"drift {diff_w:+.3%} > band {band:.3%}"
    else:
        want = int(cur_qty) if exiting else math.floor(-diff_w * nav / price)
        avail = int(pos.qty_available) if pos else 0
        side, qty, reason = "sell", min(want, avail), ("exit" if exiting else f"drift {diff_w:+.3%} > band {band:.3%}")
    if qty <= 0:
        return None
    if qty * price < limits.min_order_notional and not exiting:
        skipped.append(f"{sym}: notional {qty * price:.0f} < min {limits.min_order_notional}")
        return None
    if qty * price > limits.max_order_notional:
        qty, reason = math.floor(limits.max_order_notional / price), reason + ";capped_max_order_notional"
    return PlannedOrder(sym, side, qty, price, reason)


def _fit_budget(orders: list[PlannedOrder], budget: float, limits: Limits, tag: str, skipped: list[str]) -> tuple[list[PlannedOrder], float]:
    chosen, spent = [], 0.0
    for o in sorted(orders, key=lambda o: -o.notional):
        if spent + o.notional <= budget:
            chosen.append(o)
            spent += o.notional
            continue
        q = math.floor((budget - spent) / o.est_price)
        if q > 0 and q * o.est_price >= limits.min_order_notional:
            chosen.append(replace(o, qty=q, reason=o.reason + f";{tag}"))
            spent += q * o.est_price
        else:
            skipped.append(f"{o.symbol}: dropped by {tag}")
    return chosen, spent


def plan_orders(targets: Mapping[str, float], positions: Mapping[str, PositionSnap], prices: Mapping[str, float],
                nav: float, cash: float, limits: Limits, *, gross_scale: float = 1.0, allow_buys: bool = True,
                allow_sells: bool = True, ignore_turnover_cap: bool = False) -> Plan:
    if nav <= 0:
        raise ValueError("nav must be positive")
    skipped: list[str] = []
    tgt = _clip_targets(targets, limits, gross_scale, skipped)
    if len(tgt) > limits.universe_max_names:
        raise ValueError(f"{len(tgt)} targets > universe_max_names {limits.universe_max_names}")
    cands: list[PlannedOrder] = []
    for sym in sorted(set(tgt) | set(positions)):
        price = prices.get(sym)
        if not price or price <= 0:
            skipped.append(f"{sym}: no price")
            continue
        c = _candidate(sym, tgt.get(sym, 0.0), positions.get(sym), price, nav, limits, skipped)
        if c:
            cands.append(c)
    sells = [c for c in cands if c.side == "sell"] if allow_sells else []
    buys = [c for c in cands if c.side == "buy"] if allow_buys else []
    if not allow_sells:
        skipped.extend(f"{c.symbol}: sells disabled" for c in cands if c.side == "sell")
    if not allow_buys:
        skipped.extend(f"{c.symbol}: buys disabled (breaker)" for c in cands if c.side == "buy")

    turnover_budget = float("inf") if ignore_turnover_cap else limits.max_turnover_per_day * nav
    sells, sold = _fit_budget(sells, turnover_budget, limits, "turnover_cap", skipped)
    cash_budget = max(0.0, cash) + SELL_PROCEEDS_HAIRCUT * sold
    buys, _ = _fit_budget(buys, min(turnover_budget - sold, cash_budget), limits, "cash_or_turnover_cap", skipped)
    chosen = (sells + buys)[: limits.max_orders_per_run]
    if len(sells) + len(buys) > limits.max_orders_per_run:
        skipped.append(f"order count capped at {limits.max_orders_per_run}")
    turnover = sum(o.notional for o in chosen) / nav
    return Plan(tuple(chosen), tuple(skipped), nav, turnover, gross_scale)
