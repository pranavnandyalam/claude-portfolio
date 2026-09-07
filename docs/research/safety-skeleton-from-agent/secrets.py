"""Credential handling. Rules: load from env or .env only; never into os.environ; never printed;
a logging filter redacts the raw values if they ever reach a log line."""
from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from dotenv import dotenv_values

KEY_NAMES = ("ALPACA_API_KEY", "APCA_API_KEY_ID")
SECRET_NAMES = ("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")
_MIN_REDACT_LEN = 6


class MissingCredentials(RuntimeError):
    pass


class Secret:
    """Opaque wrapper; only the broker adapter calls .reveal()."""

    __slots__ = ("_v",)

    def __init__(self, value: str) -> None:
        self._v = value

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "Secret(***)"

    def __str__(self) -> str:
        return "***"


@dataclass(frozen=True)
class AlpacaCreds:
    key_id: Secret
    secret_key: Secret
    source: str  # "env" or the .env path; safe to log

    def fingerprint(self) -> str:
        """Safe-to-log identifier for which key is in use (first 2 + last 3 chars of the key ID)."""
        k = self.key_id.reveal()
        return f"{k[:2]}...{k[-3:]}" if len(k) >= 8 else "(short)"


class RedactingFilter(logging.Filter):
    def __init__(self, values: Iterable[str]) -> None:
        super().__init__()
        self._vals = tuple(v for v in values if v and len(v) >= _MIN_REDACT_LEN)

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if any(v in msg for v in self._vals):
            for v in self._vals:
                msg = msg.replace(v, "***")
            record.msg, record.args = msg, ()
        return True

    def redact(self, text: str) -> str:
        for v in self._vals:
            text = text.replace(v, "***")
        return text


def install_redaction(values: Iterable[str]) -> RedactingFilter:
    flt = RedactingFilter(values)
    root = logging.getLogger()
    root.addFilter(flt)
    for handler in root.handlers:
        handler.addFilter(flt)
    return flt


def load_alpaca_creds(env_file: Optional[Path] = None, env: Mapping[str, str] = os.environ) -> AlpacaCreds:
    file_vals: dict[str, str] = {}
    if env_file and env_file.exists():
        mode = stat.S_IMODE(env_file.stat().st_mode)
        if mode & 0o077:
            logging.getLogger(__name__).warning("%s is group/world readable (mode %o); run chmod 600", env_file, mode)
        file_vals = {k: v for k, v in dotenv_values(env_file).items() if v}

    def pick(names: tuple[str, ...]) -> tuple[Optional[str], str]:
        for n in names:
            if env.get(n):
                return env[n], "env"
            if file_vals.get(n):
                return file_vals[n], str(env_file)
        return None, ""

    key, src = pick(KEY_NAMES)
    sec, _ = pick(SECRET_NAMES)
    if not key or not sec:
        raise MissingCredentials(f"set {KEY_NAMES[0]} and {SECRET_NAMES[0]} in the environment or {env_file}")
    install_redaction([key, sec])
    return AlpacaCreds(Secret(key), Secret(sec), source=src)
