"""Drawdown circuit breaker + daily loss halt. Pure function of (state, equity, last_equity, today).
Levels (from peak equity):
  L1 dd>=5%  : gross exposure scaled to 50%; auto-resets when dd < l1_reset_dd (hysteresis)
  L2 dd>=10% : sells only, no buys; auto-resets to <=L1 after l2_reset_sessions consecutive sessions with dd < 10%
  L3 dd>=15% : flatten everything, hard halt; sticky until a HUMAN drops state/BREAKER_RESET (Claude may only propose)
Daily (vs last_equity): <= -3% no new orders today; <= -5% also cancel all open orders."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .config import Limits

RESET_FILE = "BREAKER_RESET"


@dataclass(frozen=True)
class BreakerState:
    peak_equity: float
    level: int = 0
    level_since: Optional[str] = None
    sessions_below_l2: int = 0
    last_reset_by: Optional[str] = None

    @classmethod
    def load(cls, path: Path, starting_equity: float) -> "BreakerState":
        if not path.exists():
            return cls(peak_equity=starting_equity)
        return cls(**json.loads(path.read_text()))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=1))
        os.replace(tmp, path)


@dataclass(frozen=True)
class Decision:
    level: int
    drawdown: float
    daily_pnl: float
    allow_buys: bool
    allow_sells: bool
    flatten: bool
    gross_scale: float
    cancel_open: bool
    halt_new_orders: bool
    reasons: tuple[str, ...]


def _consume_reset(reset_file: Optional[Path]) -> Optional[str]:
    """A human resets L3 by writing 'L3 <YYYY-MM-DD> <name> <reason>' to state/BREAKER_RESET."""
    if reset_file is None or not reset_file.exists():
        return None
    text = reset_file.read_text().strip()
    if not text.startswith("L3 "):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.replace(reset_file, reset_file.with_name(f"{reset_file.name}.consumed-{stamp}"))
    return text


def evaluate(state: BreakerState, equity: float, last_equity: float, today: date, limits: Limits,
             reset_file: Optional[Path] = None) -> tuple[BreakerState, Decision]:
    peak = max(state.peak_equity, equity)
    dd = 1.0 - equity / peak if peak > 0 else 0.0
    daily = equity / last_equity - 1.0 if last_equity > 0 else 0.0
    reasons: list[str] = []
    level, sessions_below, reset_by = state.level, state.sessions_below_l2, state.last_reset_by

    if state.level == 3:
        token = _consume_reset(reset_file)
        if token:
            level, reset_by = 0, token
            reasons.append(f"L3 reset by human: {token}")
        else:
            reasons.append("L3 sticky: waiting for human BREAKER_RESET")
    elif state.level == 2:
        if dd < limits.dd_l2:
            sessions_below += 1
            if sessions_below >= limits.l2_reset_sessions:
                level, sessions_below = (1 if dd >= limits.l1_reset_dd else 0), 0
                reasons.append(f"L2 released after {limits.l2_reset_sessions} sessions below {limits.dd_l2:.0%}")
        else:
            sessions_below = 0
    elif state.level == 1 and dd < limits.l1_reset_dd:
        level = 0
        reasons.append(f"L1 released: dd {dd:.2%} < {limits.l1_reset_dd:.0%}")

    if dd >= limits.dd_l3 and level < 3:
        level, reasons = 3, reasons + [f"L3: drawdown {dd:.2%} >= {limits.dd_l3:.0%}"]
    elif dd >= limits.dd_l2 and level < 2:
        level, sessions_below, reasons = 2, 0, reasons + [f"L2: drawdown {dd:.2%} >= {limits.dd_l2:.0%}"]
    elif dd >= limits.dd_l1 and level < 1:
        level, reasons = 1, reasons + [f"L1: drawdown {dd:.2%} >= {limits.dd_l1:.0%}"]

    cancel_open = daily <= -limits.daily_loss_cancel
    halt_daily = daily <= -limits.daily_loss_halt
    if halt_daily:
        reasons.append(f"daily loss {daily:.2%} breaches {'-' + format(limits.daily_loss_cancel, '.0%') if cancel_open else '-' + format(limits.daily_loss_halt, '.0%')}")

    new_state = replace(state, peak_equity=peak, level=level, sessions_below_l2=sessions_below,
                        level_since=(state.level_since if level == state.level else today.isoformat()),
                        last_reset_by=reset_by)
    decision = Decision(
        level=level, drawdown=dd, daily_pnl=daily,
        allow_buys=(level == 0 or level == 1) and not halt_daily,
        allow_sells=level != 3 and not halt_daily,  # at L3 the flatten path handles sells explicitly
        flatten=(level == 3 and not reset_by),
        gross_scale=1.0 if level == 0 else 0.5,
        cancel_open=cancel_open,
        halt_new_orders=halt_daily or (level == 3 and not reset_by),
        reasons=tuple(reasons),
    )
    return new_state, decision
