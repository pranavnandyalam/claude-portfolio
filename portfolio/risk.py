"""Deterministic risk limits. These run BEFORE any order is placed and cannot be overridden by
an LLM: the engine consults RiskVerdict.halts and refuses to trade when non-empty.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from portfolio.broker import Account


@dataclass(frozen=True)
class RiskLimits:
    max_position_weight: float = 0.35     # any single line (ETF sleeves may be large)
    max_gross_exposure: float = 1.0       # long-only, no leverage
    max_daily_loss_pct: float = 0.03      # equity vs previous close → halt new buys
    warn_drawdown_pct: float = 0.10       # from peak equity → warning
    max_drawdown_pct: float = 0.15        # from peak equity → circuit breaker
    max_turnover_pct: float = 0.60        # per run, fraction of equity
    max_stale_bdays: int = 3
    max_abs_daily_return: float = 0.50    # per-symbol data sanity
    min_equity_usd: float = 1_000.0
    max_symbols: int = 60


@dataclass(frozen=True)
class RiskVerdict:
    ok: bool
    halts: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    targets: dict[str, float] = field(default_factory=dict)
    drawdown_pct: float = 0.0
    daily_pnl_pct: float = 0.0
    circuit_breaker: bool = False

    @property
    def summary(self) -> str:
        parts = [f"ok={self.ok}", f"dd={self.drawdown_pct:.1%}", f"day={self.daily_pnl_pct:+.2%}"]
        if self.halts:
            parts.append("HALTS: " + "; ".join(self.halts))
        if self.warnings:
            parts.append("warnings: " + "; ".join(self.warnings))
        return " | ".join(parts)


def peak_equity(history: list[tuple[str, float, float]], current: float) -> float:
    return max([current] + [e for _, e, _ in history])


def apply_position_limits(targets: dict[str, float], limits: RiskLimits) -> tuple[dict[str, float], list[str]]:
    """Clip each weight to max_position_weight and total to max_gross_exposure. Excess is left
    in cash (never redistributed — redistribution can concentrate risk unexpectedly)."""
    warnings: list[str] = []
    clipped = {}
    for sym, w in targets.items():
        w = max(0.0, float(w))
        if w > limits.max_position_weight + 1e-9:
            warnings.append(f"{sym} weight {w:.1%} clipped to {limits.max_position_weight:.0%}")
            w = limits.max_position_weight
        if w > 0:
            clipped[sym] = w
    total = sum(clipped.values())
    if total > limits.max_gross_exposure + 1e-9:
        warnings.append(f"gross {total:.1%} scaled to {limits.max_gross_exposure:.0%}")
        clipped = {s: w * limits.max_gross_exposure / total for s, w in clipped.items()}
    if len(clipped) > limits.max_symbols:
        warnings.append(f"{len(clipped)} symbols exceeds max {limits.max_symbols}; keeping largest")
        clipped = dict(sorted(clipped.items(), key=lambda kv: -kv[1])[: limits.max_symbols])
    return clipped, warnings


def evaluate(
    targets: dict[str, float],
    account: Account,
    peak: float,
    limits: RiskLimits,
    kill_switch_active: bool = False,
    data_issues: list[str] | None = None,
    risk_off_symbol: str | None = None,
) -> RiskVerdict:
    """Combine account-state checks with target sanitization.

    Circuit breaker (drawdown >= max_drawdown_pct): targets become 100% risk_off_symbol (or
    all cash) and the verdict flags circuit_breaker=True so the engine sets the kill switch.
    """
    halts: list[str] = []
    warnings: list[str] = []
    equity = float(account.equity)
    dd = 0.0 if peak <= 0 else max(0.0, 1.0 - equity / peak)
    day = 0.0 if account.last_equity <= 0 else equity / account.last_equity - 1.0

    if kill_switch_active:
        halts.append("kill switch active")
    if account.status.upper() not in ("ACTIVE",):
        halts.append(f"account status {account.status}")
    if equity < limits.min_equity_usd:
        halts.append(f"equity ${equity:,.0f} below minimum ${limits.min_equity_usd:,.0f}")
    for issue in data_issues or []:
        halts.append(f"data: {issue}")
    if day <= -limits.max_daily_loss_pct:
        halts.append(f"daily loss {day:.2%} breaches {-limits.max_daily_loss_pct:.0%}")
    if dd >= limits.warn_drawdown_pct:
        warnings.append(f"drawdown {dd:.1%} >= warn level {limits.warn_drawdown_pct:.0%}")

    breaker = dd >= limits.max_drawdown_pct
    if breaker:
        halts.append(f"CIRCUIT BREAKER: drawdown {dd:.1%} >= {limits.max_drawdown_pct:.0%} → de-risk and halt")
        safe = {risk_off_symbol: limits.max_gross_exposure} if risk_off_symbol else {}
        return RiskVerdict(ok=False, halts=tuple(halts), warnings=tuple(warnings), targets=safe,
                           drawdown_pct=dd, daily_pnl_pct=day, circuit_breaker=True)

    clipped, w2 = apply_position_limits(targets, limits)
    warnings.extend(w2)
    return RiskVerdict(ok=not halts, halts=tuple(halts), warnings=tuple(warnings), targets=clipped,
                       drawdown_pct=dd, daily_pnl_pct=day, circuit_breaker=False)
