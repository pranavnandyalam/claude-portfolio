"""Double-run prevention (O_EXCL lock file with stale-lock reclaim) and the per-day run state machine.
State is persisted after every transition so a crashed/killed run resumes at reconcile, never re-plans."""
from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional


class AlreadyRunning(RuntimeError):
    pass


class IllegalTransition(RuntimeError):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    """Single-host lock. A lock is stale if its pid is dead or it is older than stale_after_hours."""

    def __init__(self, path: Path, stale_after_hours: float = 6.0) -> None:
        self.path = path
        self.stale_s = stale_after_hours * 3600

    def _create(self) -> None:
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "host": socket.gethostname(), "started": time.time()}, f)

    def _is_stale(self) -> bool:
        try:
            info = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return True
        age = time.time() - float(info.get("started", 0))
        return not _pid_alive(int(info.get("pid", -1))) or age > self.stale_s

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._create()
        except FileExistsError:
            if not self._is_stale():
                raise AlreadyRunning(f"another run holds {self.path}")
            self.path.unlink(missing_ok=True)
            try:
                self._create()
            except FileExistsError:
                raise AlreadyRunning(f"lost lock race on {self.path}")
        return self

    def __exit__(self, *exc: object) -> bool:
        self.path.unlink(missing_ok=True)
        return False


class Phase(str, Enum):
    INIT = "init"
    PREFLIGHT = "preflight"
    PLANNED = "planned"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    RECONCILED = "reconciled"
    DONE = "done"
    HALTED = "halted"
    FAILED = "failed"


TRANSITIONS: dict[Phase, frozenset[Phase]] = {
    Phase.INIT: frozenset({Phase.PREFLIGHT, Phase.FAILED, Phase.HALTED}),
    Phase.PREFLIGHT: frozenset({Phase.PLANNED, Phase.DONE, Phase.HALTED, Phase.FAILED}),
    Phase.PLANNED: frozenset({Phase.SUBMITTING, Phase.DONE, Phase.HALTED, Phase.FAILED}),
    Phase.SUBMITTING: frozenset({Phase.SUBMITTED, Phase.HALTED, Phase.FAILED}),
    Phase.SUBMITTED: frozenset({Phase.RECONCILED, Phase.FAILED}),
    Phase.RECONCILED: frozenset({Phase.DONE, Phase.HALTED}),
    Phase.DONE: frozenset(),
    Phase.HALTED: frozenset(),
    Phase.FAILED: frozenset({Phase.PREFLIGHT}),
}
# A run interrupted in these phases must NOT re-plan: orders may already be live. Resume at reconcile.
RESUME_AT_RECONCILE = frozenset({Phase.SUBMITTING, Phase.SUBMITTED})


@dataclass(frozen=True)
class RunState:
    run_date: str
    run_id: str
    phase: Phase = Phase.INIT
    attempt: int = 1
    plan_hash: Optional[str] = None
    submitted_coids: tuple[str, ...] = ()
    history: tuple[dict, ...] = ()
    note: str = ""

    @staticmethod
    def path_for(state_dir: Path, run_date: str) -> Path:
        return state_dir / "runs" / f"{run_date}.json"

    @classmethod
    def load(cls, path: Path) -> Optional["RunState"]:
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
        raw["phase"] = Phase(raw["phase"])
        raw["submitted_coids"] = tuple(raw.get("submitted_coids", ()))
        raw["history"] = tuple(raw.get("history", ()))
        return cls(**raw)

    def advance(self, new: Phase, note: str = "") -> "RunState":
        if new not in TRANSITIONS[self.phase]:
            raise IllegalTransition(f"{self.phase.value} -> {new.value} not allowed")
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "from": self.phase.value, "to": new.value, "note": note}
        return replace(self, phase=new, note=note, history=self.history + (entry,))

    def with_plan(self, plan_hash: str) -> "RunState":
        if self.plan_hash and self.plan_hash != plan_hash:
            raise IllegalTransition(f"plan hash changed within a run ({self.plan_hash} -> {plan_hash}); refusing")
        return replace(self, plan_hash=plan_hash)

    def with_submitted(self, coid: str) -> "RunState":
        return replace(self, submitted_coids=self.submitted_coids + ((coid,) if coid not in self.submitted_coids else ()))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        data = asdict(self)
        data["phase"] = self.phase.value
        tmp.write_text(json.dumps(data, indent=1, default=str))
        os.replace(tmp, path)
