"""Heartbeat + alert conditions. The notifier (Claude session / Telegram / PushNotification) reads these; the
engine never sends messages itself. Rule from the post-mortems: absence of a run is itself an alert."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Alert:
    severity: str  # "page" | "warn" | "info"
    code: str
    detail: str


def write_heartbeat(path: Path, **fields: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **fields}, default=str, indent=1))
    os.replace(tmp, path)


def alert_conditions(*, auth_failed: bool = False, data_stale: bool = False, preflight_failed: bool = False,
                     breaker_level: int = 0, daily_halt: bool = False, mismatches: int = 0, rejected: int = 0,
                     partial_fills: int = 0, missed_sessions: int = 0, lock_conflict: bool = False,
                     audit_chain_ok: bool = True, last_run_phase: Optional[str] = None) -> tuple[Alert, ...]:
    out: list[Alert] = []
    if auth_failed:
        out.append(Alert("page", "ALPACA_AUTH", "401 from Alpaca: regenerate paper keys at app.alpaca.markets and update .env"))
    if breaker_level >= 3:
        out.append(Alert("page", "BREAKER_L3", "flattened + halted; human must write state/BREAKER_RESET"))
    if mismatches:
        out.append(Alert("page", "POSITION_MISMATCH", f"{mismatches} expected/actual mismatches after reconcile"))
    if missed_sessions:
        out.append(Alert("page", "MISSED_RUN", f"{missed_sessions} trading session(s) with no completed run"))
    if not audit_chain_ok:
        out.append(Alert("page", "AUDIT_CHAIN", "audit log hash chain broken"))
    if last_run_phase in ("failed", "halted"):
        out.append(Alert("warn", "RUN_" + last_run_phase.upper(), f"last run ended in {last_run_phase}"))
    if daily_halt:
        out.append(Alert("warn", "DAILY_LOSS_HALT", "no new orders today"))
    if breaker_level in (1, 2):
        out.append(Alert("warn", f"BREAKER_L{breaker_level}", "de-risked" if breaker_level == 1 else "sells only"))
    if data_stale or preflight_failed:
        out.append(Alert("warn", "PREFLIGHT", "data stale or preflight failed; run skipped"))
    if rejected:
        out.append(Alert("warn", "ORDERS_REJECTED", f"{rejected} order(s) rejected by broker"))
    if partial_fills:
        out.append(Alert("info", "PARTIAL_FILLS", f"{partial_fills} partial fill(s) rolled to next run"))
    if lock_conflict:
        out.append(Alert("warn", "LOCK_CONFLICT", "a run was skipped because another was in progress"))
    return tuple(out)
