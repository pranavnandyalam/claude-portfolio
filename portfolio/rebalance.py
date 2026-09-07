"""Turn target weights into a concrete, safe order plan against current positions.

Rules
- Long-only. Targets are clipped to [0, 1] and must sum to <= 1.
- Drift bands: a line is traded only if |target - current| exceeds `drift_band_abs` of equity,
  except full exits (target 0 while holding) which always execute.
- Sells are qty-based (never over-sell); buys are notional-based (fractional) or whole-share
  for non-fractionable assets.
- Sells are listed before buys; buys are scaled down if they would exceed cash + sell proceeds.
- Pure function: no I/O, no broker calls. The executor applies the plan.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from portfolio.broker import MIN_NOTIONAL_USD, Account, Position, round_qty

FULL_EXIT_EPS = 1e-6


@dataclass(frozen=True)
class RebalanceParams:
    drift_band_abs: float = 0.02      # fraction of equity, on rebalance days
    mid_period_drift_band: float = 0.05  # between rebalance days: only fix large drift
    min_trade_usd: float = 25.0
    cash_buffer_pct: float = 0.01     # fraction of equity kept uninvested
    sell_proceeds_haircut: float = 0.005  # assume 0.5% less cash from sells than marked
    max_turnover_pct: float = 1.0     # hard cap on |traded notional| / equity per run


@dataclass(frozen=True)
class PlannedTrade:
    symbol: str
    side: str                 # buy | sell
    notional: float           # positive USD (estimated for qty trades)
    qty: float | None         # set for sells and non-fractionable buys
    current_weight: float
    target_weight: float
    reason: str
    client_order_id: str = ""


@dataclass(frozen=True)
class OrderPlan:
    run_date: str
    equity: float
    cash: float
    trades: tuple[PlannedTrade, ...]
    skipped: tuple[tuple[str, str], ...]
    target_weights: dict[str, float]
    current_weights: dict[str, float]
    turnover_usd: float
    blocked_reason: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def turnover_pct(self) -> float:
        return self.turnover_usd / self.equity if self.equity else 0.0

    @property
    def sells(self) -> list[PlannedTrade]:
        return [t for t in self.trades if t.side == "sell"]

    @property
    def buys(self) -> list[PlannedTrade]:
        return [t for t in self.trades if t.side == "buy"]

    @property
    def is_empty(self) -> bool:
        return not self.trades


def normalize_targets(targets: dict[str, float]) -> dict[str, float]:
    """Clip negatives, drop zeros/NaN, scale down (never up) if sum > 1."""
    clean = {s.upper(): float(w) for s, w in targets.items() if w is not None and not math.isnan(float(w)) and float(w) > 0}
    total = sum(clean.values())
    if total > 1.0 + 1e-9:
        clean = {s: w / total for s, w in clean.items()}
    return clean


def build_order_plan(
    targets: dict[str, float],
    positions: list[Position],
    account: Account,
    prices: dict[str, float],
    run_date: str,
    params: RebalanceParams = RebalanceParams(),
    fractionable: Callable[[str], bool] | None = None,
    client_id_fn: Callable[[str, str], str] | None = None,
) -> OrderPlan:
    targets = normalize_targets(targets)
    equity = float(account.equity)
    cash = float(account.cash)
    if equity <= 0:
        return OrderPlan(run_date, equity, cash, (), (), targets, {}, 0.0, blocked_reason="non-positive equity")
    investable = equity * (1.0 - params.cash_buffer_pct)
    pos_by_sym = {p.symbol.upper(): p for p in positions}
    current_w = {s: p.market_value / equity for s, p in pos_by_sym.items()}
    is_frac = fractionable or (lambda _s: True)
    cid = client_id_fn or (lambda _s, _side: "")

    sells: list[PlannedTrade] = []
    buys: list[PlannedTrade] = []
    skipped: list[tuple[str, str]] = []
    notes: list[str] = []

    for sym in sorted(set(targets) | set(pos_by_sym)):
        tw = targets.get(sym, 0.0)
        pos = pos_by_sym.get(sym)
        cur_val = pos.market_value if pos else 0.0
        cur_w = cur_val / equity
        price = prices.get(sym)
        if price is None or price <= 0:
            if tw > 0 or pos:
                skipped.append((sym, "no price"))
            continue
        if pos and pos.qty < 0:
            skipped.append((sym, "short position present; manual attention"))
            continue

        target_val = tw * investable
        diff = target_val - cur_val

        # Full exit: always execute regardless of band.
        if tw <= 0 and pos and pos.qty > FULL_EXIT_EPS:
            sells.append(PlannedTrade(sym, "sell", abs(cur_val), round_qty(pos.qty), cur_w, 0.0, "exit", cid(sym, "sell")))
            continue
        if abs(diff) / equity < params.drift_band_abs:
            if tw > 0 or pos:
                skipped.append((sym, f"within band ({diff / equity:+.2%})"))
            continue
        if abs(diff) < max(params.min_trade_usd, MIN_NOTIONAL_USD):
            skipped.append((sym, f"below min trade (${abs(diff):.0f})"))
            continue

        if diff < 0:
            qty = round_qty(min(pos.qty, abs(diff) / price)) if pos else 0.0
            if pos and pos.qty - qty < 1e-4:  # would leave dust → sell everything
                qty = round_qty(pos.qty)
            if not is_frac(sym):
                qty = float(math.floor(qty))
            if qty <= 0:
                skipped.append((sym, "sell qty rounds to zero"))
                continue
            sells.append(PlannedTrade(sym, "sell", qty * price, qty, cur_w, tw, "reduce", cid(sym, "sell")))
        else:
            if is_frac(sym):
                buys.append(PlannedTrade(sym, "buy", round(diff, 2), None, cur_w, tw, "increase" if pos else "enter", cid(sym, "buy")))
            else:
                qty = float(math.floor(diff / price))
                if qty <= 0:
                    skipped.append((sym, "non-fractionable; less than one share"))
                    continue
                buys.append(PlannedTrade(sym, "buy", qty * price, qty, cur_w, tw, "increase" if pos else "enter", cid(sym, "buy")))

    # Cash feasibility: buys cannot exceed cash + haircut sell proceeds.
    available = cash + sum(t.notional for t in sells) * (1.0 - params.sell_proceeds_haircut)
    total_buys = sum(t.notional for t in buys)
    if total_buys > available and total_buys > 0:
        scale = max(0.0, available / total_buys)
        notes.append(f"buys scaled by {scale:.3f} to fit cash (${available:,.0f} available vs ${total_buys:,.0f} requested)")
        scaled: list[PlannedTrade] = []
        for t in buys:
            n = round(t.notional * scale, 2)
            if n < max(params.min_trade_usd, MIN_NOTIONAL_USD):
                skipped.append((t.symbol, "scaled buy below min trade"))
                continue
            q = None if t.qty is None else float(math.floor(n / prices[t.symbol]))
            if t.qty is not None and (q or 0) <= 0:
                skipped.append((t.symbol, "scaled non-fractionable buy < 1 share"))
                continue
            scaled.append(PlannedTrade(t.symbol, t.side, n if q is None else q * prices[t.symbol], q, t.current_weight, t.target_weight, t.reason, t.client_order_id))
        buys = scaled

    trades = tuple(sells + buys)
    turnover = sum(t.notional for t in trades)
    blocked = ""
    if equity and turnover / equity > params.max_turnover_pct + 1e-9:
        blocked = f"turnover {turnover / equity:.1%} exceeds cap {params.max_turnover_pct:.0%}"
    return OrderPlan(run_date, equity, cash, trades, tuple(skipped), targets, current_w, turnover, blocked, tuple(notes))


def plan_to_rows(plan: OrderPlan) -> list[dict]:
    return [
        {"symbol": t.symbol, "side": t.side, "notional": round(t.notional, 2), "qty": t.qty,
         "current_w": round(t.current_weight, 4), "target_w": round(t.target_weight, 4), "reason": t.reason}
        for t in plan.trades
    ]
