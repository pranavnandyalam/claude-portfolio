"""Deterministic identifiers. One order per (run_date, symbol) per day; the hash covers the frozen plan so a resumed
run regenerates the identical client_order_id. Alpaca limit: <= 128 chars, unique per active order (422 otherwise)."""
from __future__ import annotations

import hashlib
from datetime import date

COID_PREFIX = "cp"
MAX_COID_LEN = 128


def coid_prefix(run_date: date, symbol: str) -> str:
    return f"{COID_PREFIX}-{run_date:%Y%m%d}-{symbol}-"


def client_order_id(run_date: date, symbol: str, side: str, qty: int, strategy_version: str) -> str:
    if qty <= 0 or int(qty) != qty:
        raise ValueError(f"qty must be a positive whole number, got {qty!r}")
    payload = "|".join([strategy_version, run_date.isoformat(), symbol, side.lower(), str(int(qty))])
    digest = hashlib.sha256(payload.encode()).hexdigest()[:10]
    coid = f"{coid_prefix(run_date, symbol)}{side[0].lower()}{int(qty)}-{digest}"
    if len(coid) > MAX_COID_LEN:
        raise ValueError(f"client_order_id too long: {coid}")
    return coid


def run_id(run_date: date, attempt: int) -> str:
    return f"run-{run_date:%Y%m%d}-{attempt:02d}"
