"""Preflight checks. Every check returns a CheckResult; any failed 'halt' check stops the run before planning.
Staleness is measured against the exchange calendar (last session <= today), never against the wall clock."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Iterable, Mapping, Optional, Sequence

from .broker import AccountSnap, ClockSnap, SessionDay

WINDOW_START = time(9, 35)   # after the opening auction settles
WINDOW_END = time(15, 45)    # leave time for fills + reconcile before the close


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    severity: str = "halt"  # "halt" | "warn"
    detail: str = ""


def last_session_on_or_before(sessions: Sequence[SessionDay], d: date) -> Optional[date]:
    days = [s.day for s in sessions if s.day <= d]
    return max(days) if days else None


def is_session(sessions: Sequence[SessionDay], d: date) -> bool:
    return any(s.day == d for s in sessions)


def check_is_trading_day(sessions: Sequence[SessionDay], today: date) -> CheckResult:
    ok = is_session(sessions, today)
    return CheckResult("calendar.trading_day", ok, "halt", f"{today} {'is' if ok else 'is not'} a session")


def check_bars_fresh(last_bar: Optional[date], expected: Optional[date]) -> CheckResult:
    if last_bar is None or expected is None:
        return CheckResult("data.fresh", False, "halt", "no bars or no calendar available")
    return CheckResult("data.fresh", last_bar >= expected, "halt", f"last_bar={last_bar} expected>={expected}")


def check_prices_finite(prices: Mapping[str, Optional[float]]) -> CheckResult:
    bad = sorted(s for s, p in prices.items() if p is None or not math.isfinite(p) or p <= 0)
    return CheckResult("data.prices_finite", not bad, "halt", f"bad={bad}")


def check_price_agreement(ref: Mapping[str, float], broker: Mapping[str, float], tol: float) -> CheckResult:
    """yfinance close vs broker current price; large gaps mean a stale/adjusted/mis-mapped series."""
    diffs = {s: abs(ref[s] / broker[s] - 1.0) for s in ref if broker.get(s, 0) > 0}
    bad = {s: round(d, 4) for s, d in diffs.items() if d > tol}
    return CheckResult("data.price_agreement", not bad, "halt", f"tol={tol} bad={bad}")


def check_missing_symbols(needed: Iterable[str], have: Mapping[str, object]) -> CheckResult:
    missing = sorted(s for s in needed if s not in have)
    return CheckResult("data.coverage", not missing, "halt", f"missing={missing}")


def check_account(acct: AccountSnap) -> CheckResult:
    problems = []
    if acct.status.upper() != "ACTIVE":
        problems.append(f"status={acct.status}")
    if acct.trading_blocked:
        problems.append("trading_blocked")
    if acct.account_blocked:
        problems.append("account_blocked")
    if acct.equity <= 0:
        problems.append("equity<=0")
    return CheckResult("account.ok", not problems, "halt", ",".join(problems))


def check_trading_window(clock: ClockSnap, now_et: datetime, start: time = WINDOW_START, end: time = WINDOW_END) -> CheckResult:
    inside = clock.is_open and start <= now_et.time() <= end
    return CheckResult("clock.window", inside, "halt", f"is_open={clock.is_open} now_et={now_et.time()} window={start}-{end}")


def check_universe_tradable(targets: Mapping[str, float], tradable: Mapping[str, bool]) -> CheckResult:
    bad = sorted(s for s in targets if not tradable.get(s, False))
    return CheckResult("universe.tradable", not bad, "halt", f"not_tradable={bad}")


def summarize(results: Iterable[CheckResult]) -> tuple[bool, tuple[CheckResult, ...]]:
    rs = tuple(results)
    return all(r.ok or r.severity != "halt" for r in rs), rs
