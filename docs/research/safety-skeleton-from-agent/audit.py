"""Append-only JSONL audit log with a hash chain (tamper-evident) and secret redaction.
One line per event: {ts, run_id, seq, event, level, payload, prev, hash}. fsync'd on every write."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

GENESIS = "0" * 16


def _hash(record: dict) -> str:
    body = json.dumps({k: v for k, v in record.items() if k != "hash"}, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


class AuditLog:
    def __init__(self, path: Path, run_id: str, redactor: Optional[Callable[[str], str]] = None) -> None:
        self.path, self.run_id, self._redact = path, run_id, redactor
        self._seq = 0
        self._prev = self._last_hash()

    def _last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = ""
        with open(self.path, "rb") as f:
            for line in f:
                if line.strip():
                    last = line.decode()
        return json.loads(last)["hash"] if last else GENESIS

    def write(self, event: str, level: str = "info", **payload: object) -> dict:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "run_id": self.run_id, "seq": self._seq,
               "event": event, "level": level, "payload": payload, "prev": self._prev}
        if self._redact:  # redact BEFORE hashing so the stored hash covers exactly what is on disk
            rec = json.loads(self._redact(json.dumps(rec, sort_keys=True, default=str)))
        rec["hash"] = _hash(rec)
        line = json.dumps(rec, sort_keys=True, default=str)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._prev, self._seq = rec["hash"], self._seq + 1
        return rec

    @staticmethod
    def verify_chain(path: Path) -> tuple[bool, int]:
        prev, n = GENESIS, 0
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec["prev"] != prev or _hash(rec) != rec["hash"]:
                return False, n
            prev, n = rec["hash"], n + 1
        return True, n
