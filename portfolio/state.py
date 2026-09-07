"""Run state, locking and audit logging. Everything is plain JSON/JSONL under state/ so a
human (or a scheduled Claude session) can read it without tooling.

Files
- state/run_lock            : exclusive lock (pid + started_at); prevents double runs
- state/kill_switch.json    : {"active": bool, "reason": str, "set_at": iso, "set_by": str}
- state/last_run.json       : summary of the most recent engine run
- state/equity_history.csv  : date,equity,cash appended once per report run
- logs/audit.jsonl          : append-only structured log of every decision and order
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state"
LOG_DIR = PROJECT_ROOT / "logs"
LOCK_FILE = STATE_DIR / "run_lock"
KILL_SWITCH_FILE = STATE_DIR / "kill_switch.json"
LAST_RUN_FILE = STATE_DIR / "last_run.json"
EQUITY_HISTORY_FILE = STATE_DIR / "equity_history.csv"
TARGETS_FILE = STATE_DIR / "targets.json"
ACCOUNT_FILE = STATE_DIR / "account.json"
AUDIT_FILE = LOG_DIR / "audit.jsonl"
STALE_LOCK_SECONDS = 2 * 3600


class LockHeld(RuntimeError):
    """Another run holds the lock."""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "item"):  # numpy scalars
        try:
            return obj.item()
        except Exception:
            pass
    if isinstance(obj, float) and obj != obj:  # NaN
        return None
    return obj


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_jsonable(obj), indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


# --------------------------------------------------------------------------- lock

@contextmanager
def run_lock(lock_file: Path | None = None, stale_after: float = STALE_LOCK_SECONDS) -> Iterator[None]:
    """Exclusive lock via O_EXCL create. A lock older than stale_after is considered abandoned."""
    lock_file = lock_file or LOCK_FILE
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    if lock_file.exists():
        age = time.time() - lock_file.stat().st_mtime
        if age < stale_after:
            info = read_json(lock_file, {})
            raise LockHeld(f"run lock held by pid {info.get('pid')} since {info.get('started_at')} ({age:.0f}s ago)")
        lock_file.unlink(missing_ok=True)
    fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({"pid": os.getpid(), "started_at": utcnow_iso()}, fh)
        yield
    finally:
        lock_file.unlink(missing_ok=True)


# --------------------------------------------------------------------------- kill switch

@dataclass(frozen=True)
class KillSwitch:
    active: bool = False
    reason: str = ""
    set_at: str = ""
    set_by: str = ""


def read_kill_switch(path: Path | None = None) -> KillSwitch:
    path = path or KILL_SWITCH_FILE
    raw = read_json(path, {}) or {}
    return KillSwitch(active=bool(raw.get("active", False)), reason=str(raw.get("reason", "")),
                      set_at=str(raw.get("set_at", "")), set_by=str(raw.get("set_by", "")))


def set_kill_switch(active: bool, reason: str, set_by: str, path: Path | None = None) -> KillSwitch:
    path = path or KILL_SWITCH_FILE
    ks = KillSwitch(active=active, reason=reason, set_at=utcnow_iso(), set_by=set_by)
    write_json(path, ks)
    audit("kill_switch", active=active, reason=reason, set_by=set_by)
    return ks


# --------------------------------------------------------------------------- audit

def audit(event: str, path: Path | None = None, **fields: Any) -> dict[str, Any]:
    """Append one JSON line. Never raises (logging must not break trading)."""
    path = path or AUDIT_FILE  # resolved at call time so tests/monkeypatching work
    rec = {"ts": utcnow_iso(), "event": event, **_jsonable(fields)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError:
        pass
    return rec


def read_audit(path: Path | None = None, last_n: int | None = None, event: str | None = None) -> list[dict[str, Any]]:
    path = path or AUDIT_FILE
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    recs = []
    for line in lines:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event is None or r.get("event") == event:
            recs.append(r)
    return recs[-last_n:] if last_n else recs


# --------------------------------------------------------------------------- run summary / equity

def write_last_run(summary: dict[str, Any], path: Path | None = None) -> None:
    path = path or LAST_RUN_FILE
    write_json(path, {"written_at": utcnow_iso(), **summary})


def read_last_run(path: Path | None = None) -> dict[str, Any]:
    path = path or LAST_RUN_FILE
    return read_json(path, {}) or {}


def append_equity(date: str, equity: float, cash: float, path: Path | None = None) -> None:
    """Append one row per date (idempotent: replaces an existing row for the same date)."""
    path = path or EQUITY_HISTORY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: dict[str, tuple[float, float]] = {}
    if path.exists():
        for line in path.read_text().splitlines()[1:]:
            parts = line.split(",")
            if len(parts) >= 3:
                rows[parts[0]] = (float(parts[1]), float(parts[2]))
    rows[date] = (float(equity), float(cash))
    body = "date,equity,cash\n" + "".join(f"{d},{e:.2f},{c:.2f}\n" for d, (e, c) in sorted(rows.items()))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body)
    tmp.replace(path)


def read_equity_history(path: Path | None = None) -> list[tuple[str, float, float]]:
    path = path or EQUITY_HISTORY_FILE
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 3:
            out.append((parts[0], float(parts[1]), float(parts[2])))
    return out


# --------------------------------------------------------------------------- stored targets

def read_targets(path: Path | None = None) -> dict[str, Any]:
    """{"as_of": "YYYY-MM-DD", "period_key": str, "targets": {...}, "per_sleeve": {...}} or {}."""
    path = path or TARGETS_FILE
    return read_json(path, {}) or {}


def write_targets(as_of: str, period_key: str, targets: dict[str, float], per_sleeve: dict[str, dict[str, float]],
                  extra: dict[str, Any] | None = None, path: Path | None = None) -> None:
    path = path or TARGETS_FILE
    write_json(path, {"as_of": as_of, "period_key": period_key, "targets": targets, "per_sleeve": per_sleeve, **(extra or {})})


# --------------------------------------------------------------------------- account pinning

def check_account_pin(account_id: str, is_paper: bool, path: Path | None = None) -> str:
    """Pin the broker account on first use; afterwards refuse a different account/mode.
    Returns '' if ok, else a human-readable problem. Never trades across a silent key swap."""
    path = path or ACCOUNT_FILE
    pinned = read_json(path, {}) or {}
    if not pinned:
        write_json(path, {"account_id": account_id, "is_paper": is_paper, "pinned_at": utcnow_iso()})
        audit("account_pinned", account_id=account_id, is_paper=is_paper)
        return ""
    if pinned.get("account_id") != account_id or bool(pinned.get("is_paper")) != bool(is_paper):
        return (f"broker account changed: pinned {pinned.get('account_id')} (paper={pinned.get('is_paper')}) vs now "
                f"{account_id} (paper={is_paper}); delete state/account.json to re-pin after review")
    return ""


def mark_targets_executed(run_date: str, path: Path | None = None) -> None:
    """Record that the current period's targets were executed (so later days are 'mid-period')."""
    path = path or TARGETS_FILE
    cur = read_json(path, {}) or {}
    if cur:
        write_json(path, {**cur, "executed_on": run_date})
