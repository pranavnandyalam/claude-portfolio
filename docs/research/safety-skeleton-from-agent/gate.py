"""Paper/live gate. LIVE requires ALL of: --mode live, env sentinel, state/LIVE_TRADING_ENABLED containing the
account id, broker base_url == api.alpaca.markets, and account id == file contents. PAPER asserts the paper URL and
the 'PA' account_number prefix. Both modes pin the account id on first run and refuse silent key swaps."""
from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Mapping

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
LIVE_ENV = "CLAUDE_PORTFOLIO_LIVE"
LIVE_ENV_VALUE = "I_UNDERSTAND_THIS_IS_REAL_MONEY"
LIVE_FILE = "LIVE_TRADING_ENABLED"
PAPER_ACCOUNT_PREFIX = "PA"


class Mode(str, Enum):
    DRY_RUN = "dry-run"
    PAPER = "paper"
    LIVE = "live"


class GateRefused(RuntimeError):
    pass


def resolve_mode(cli_mode: str, state_dir: Path, env: Mapping[str, str] = os.environ) -> Mode:
    mode = Mode(cli_mode)
    if mode is not Mode.LIVE:
        return mode
    if env.get(LIVE_ENV) != LIVE_ENV_VALUE:
        raise GateRefused(f"live refused: env {LIVE_ENV} is not the sentinel value")
    live_file = state_dir / LIVE_FILE
    if not live_file.exists() or not live_file.read_text().strip():
        raise GateRefused(f"live refused: {live_file} missing or empty (must contain the live account id)")
    return mode


def expected_base_url(mode: Mode) -> str:
    return LIVE_URL if mode is Mode.LIVE else PAPER_URL


def assert_account_matches(mode: Mode, base_url: str, account_id: str, account_number: str, state_dir: Path) -> None:
    want = expected_base_url(mode)
    if str(base_url).rstrip("/") != want:
        raise GateRefused(f"base_url {base_url!r} != {want!r} for mode {mode.value}")
    if mode is Mode.LIVE:
        allowed = (state_dir / LIVE_FILE).read_text().strip()
        if account_id != allowed:
            raise GateRefused("live refused: broker account id != id in LIVE_TRADING_ENABLED")
    elif not account_number.upper().startswith(PAPER_ACCOUNT_PREFIX):
        raise GateRefused(f"paper mode but account_number {account_number!r} lacks {PAPER_ACCOUNT_PREFIX} prefix")
    pin = state_dir / f"account_id.{mode.value}.pin"
    if pin.exists():
        if pin.read_text().strip() != account_id:
            raise GateRefused(f"account id differs from pinned id in {pin}; delete the pin deliberately if keys changed")
    else:
        state_dir.mkdir(parents=True, exist_ok=True)
        pin.write_text(account_id + "\n")
