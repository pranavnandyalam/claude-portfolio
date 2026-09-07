"""Risk limits: single source of truth (config/limits.yaml), validated at startup, immutable."""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import yaml


class ConfigError(ValueError):
    pass


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


@dataclass(frozen=True)
class Limits:
    strategy_version: str = "v1"
    universe_max_names: int = 30
    max_position_weight: float = 0.10
    max_gross_exposure: float = 1.00
    max_turnover_per_day: float = 0.25
    min_order_notional: float = 50.0
    max_order_notional: float = 5000.0
    max_orders_per_run: int = 60
    drift_band_abs: float = 0.005
    drift_band_rel: float = 0.20
    price_sanity_tol: float = 0.03
    daily_loss_halt: float = 0.03
    daily_loss_cancel: float = 0.05
    dd_l1: float = 0.05
    dd_l2: float = 0.10
    dd_l3: float = 0.15
    l1_reset_dd: float = 0.03
    l2_reset_sessions: int = 5
    order_fill_wait_s: int = 900
    lock_stale_hours: float = 6.0

    def validate(self) -> "Limits":
        _require(0 < self.max_position_weight <= 0.25, "max_position_weight must be in (0, 0.25]")
        _require(0 < self.max_gross_exposure <= 1.0, "max_gross_exposure must be in (0, 1] (long-only, no margin)")
        _require(0 < self.max_turnover_per_day <= 1.0, "max_turnover_per_day must be in (0, 1]")
        _require(0 < self.min_order_notional < self.max_order_notional, "order notional bounds inverted")
        _require(0 < self.dd_l1 < self.dd_l2 < self.dd_l3 < 0.5, "drawdown levels must be increasing and < 50%")
        _require(0 < self.daily_loss_halt < self.daily_loss_cancel < 0.25, "daily loss thresholds inverted")
        _require(0 < self.l1_reset_dd < self.dd_l1, "l1_reset_dd must be below dd_l1 (hysteresis)")
        _require(0 <= self.drift_band_abs < 0.05 and 0 <= self.drift_band_rel < 1, "drift bands out of range")
        _require(0 < self.price_sanity_tol < 0.2, "price_sanity_tol out of range")
        _require(1 <= self.max_orders_per_run <= 200, "max_orders_per_run out of range")
        return self


def load_limits(path: Path) -> Limits:
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    raw = raw or {}
    known = {f.name for f in fields(Limits)}
    unknown = set(raw) - known
    _require(not unknown, f"unknown keys in {path}: {sorted(unknown)}")
    return Limits(**raw).validate()
