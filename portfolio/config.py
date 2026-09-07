"""Typed configuration loaded from config/portfolio.yaml.

Bounded autonomy: `AdaptBounds` defines the ranges within which a supervising Claude session
may change sleeve allocations/vol target without human approval. Anything outside → proposal.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from portfolio.rebalance import RebalanceParams
from portfolio.risk import RiskLimits

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "portfolio.yaml"
SLEEVE_TYPES = ("trend", "gem", "rotation", "xs_momentum", "static")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Sleeve:
    name: str
    type: str
    allocation: float
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


@dataclass(frozen=True)
class AdaptBounds:
    sleeve_allocation_min: float = 0.0
    sleeve_allocation_max: float = 0.7
    max_allocation_change_per_week: float = 0.10
    vol_target_min: float = 0.06
    vol_target_max: float = 0.14


@dataclass(frozen=True)
class Schedule:
    rebalance_freq: str = "M"          # M | W | D
    trade_time_local: str = "10:05"    # America/New_York
    report_time_local: str = "16:20"
    weekly_review_day: str = "SUN"


@dataclass(frozen=True)
class PortfolioConfig:
    name: str
    mode: str                          # paper | live
    sleeves: tuple[Sleeve, ...]
    cash_symbol: str
    vol_target: float | None
    vol_window: int
    risk: RiskLimits
    rebalance: RebalanceParams
    adapt: AdaptBounds
    schedule: Schedule
    data_start: str = "2003-01-01"
    benchmark: str = "SPY"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def enabled_sleeves(self) -> tuple[Sleeve, ...]:
        return tuple(s for s in self.sleeves if s.enabled)

    @property
    def all_symbols(self) -> list[str]:
        syms: list[str] = [self.cash_symbol, self.benchmark]
        for s in self.sleeves:
            for key in ("assets", "universe"):
                syms.extend(s.params.get(key, []))
            for key in ("us", "intl", "bonds", "tbill", "cash"):
                v = s.params.get(key)
                if isinstance(v, str):
                    syms.append(v)
            for k in s.params.get("weights", {}) or {}:
                syms.append(k)
        return list(dict.fromkeys(str(x).upper() for x in syms if x))


def _dc(cls, d: dict[str, Any] | None, what: str):
    d = dict(d or {})
    valid = set(cls.__dataclass_fields__)
    unknown = set(d) - valid
    if unknown:
        raise ConfigError(f"unknown keys in {what}: {sorted(unknown)}")
    try:
        return cls(**d)
    except TypeError as exc:
        raise ConfigError(f"bad {what}: {exc}") from exc


def validate(cfg: PortfolioConfig) -> PortfolioConfig:
    if cfg.mode not in ("paper", "live"):
        raise ConfigError("mode must be 'paper' or 'live'")
    if not cfg.sleeves:
        raise ConfigError("at least one sleeve required")
    names = [s.name for s in cfg.sleeves]
    if len(set(names)) != len(names):
        raise ConfigError("sleeve names must be unique")
    for s in cfg.sleeves:
        if s.type not in SLEEVE_TYPES:
            raise ConfigError(f"sleeve {s.name}: unknown type {s.type!r} (valid: {SLEEVE_TYPES})")
        if not 0.0 <= s.allocation <= 1.0:
            raise ConfigError(f"sleeve {s.name}: allocation must be in [0,1]")
    total = sum(s.allocation for s in cfg.enabled_sleeves)
    if total > 1.0 + 1e-9:
        raise ConfigError(f"enabled sleeve allocations sum to {total:.3f} > 1")
    if cfg.vol_target is not None and not (0.02 <= cfg.vol_target <= 0.30):
        raise ConfigError("vol_target must be between 2% and 30%")
    if cfg.schedule.rebalance_freq not in ("M", "W", "D"):
        raise ConfigError("schedule.rebalance_freq must be M, W or D")
    return cfg


def from_dict(raw: dict[str, Any]) -> PortfolioConfig:
    raw = dict(raw or {})
    sleeves = tuple(_dc(Sleeve, s, f"sleeve[{i}]") for i, s in enumerate(raw.get("sleeves", [])))
    cfg = PortfolioConfig(
        name=str(raw.get("name", "claude-portfolio")),
        mode=str(raw.get("mode", "paper")),
        sleeves=sleeves,
        cash_symbol=str(raw.get("cash_symbol", "BIL")).upper(),
        vol_target=(None if raw.get("vol_target") in (None, "", 0) else float(raw["vol_target"])),
        vol_window=int(raw.get("vol_window", 63)),
        risk=_dc(RiskLimits, raw.get("risk"), "risk"),
        rebalance=_dc(RebalanceParams, raw.get("rebalance"), "rebalance"),
        adapt=_dc(AdaptBounds, raw.get("adapt"), "adapt"),
        schedule=_dc(Schedule, raw.get("schedule"), "schedule"),
        data_start=str(raw.get("data_start", "2003-01-01")),
        benchmark=str(raw.get("benchmark", "SPY")).upper(),
        raw=raw,
    )
    return validate(cfg)


def load_config(path: Path = DEFAULT_CONFIG) -> PortfolioConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text()) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    return from_dict(raw)


def with_allocations(cfg: PortfolioConfig, allocations: dict[str, float], vol_target: float | None = None) -> PortfolioConfig:
    """Return a new config with sleeve allocations (and optionally vol_target) changed atomically.
    Immutable: the input config is untouched. Validation runs on the final state."""
    unknown = set(allocations) - {s.name for s in cfg.sleeves}
    if unknown:
        raise ConfigError(f"no sleeve named {sorted(unknown)}")
    sleeves = tuple(replace(s, allocation=float(allocations.get(s.name, s.allocation))) for s in cfg.sleeves)
    new = replace(cfg, sleeves=sleeves)
    if vol_target is not None:
        new = replace(new, vol_target=float(vol_target))
    return validate(new)


def dump_yaml(cfg: PortfolioConfig) -> str:
    """Serialize the editable parts of a config back to YAML (raw dict updated with sleeves/vol_target)."""
    raw = dict(cfg.raw)
    raw["sleeves"] = [{"name": s.name, "type": s.type, "allocation": round(s.allocation, 4), "enabled": s.enabled, "params": dict(s.params)} for s in cfg.sleeves]
    raw["vol_target"] = cfg.vol_target
    raw["mode"] = cfg.mode
    return yaml.safe_dump(raw, sort_keys=False)


def check_adaptation(old: PortfolioConfig, new: PortfolioConfig) -> list[str]:
    """Return violations if `new` differs from `old` beyond AdaptBounds (empty = allowed)."""
    b = old.adapt
    issues: list[str] = []
    old_by = {s.name: s for s in old.sleeves}
    for s in new.sleeves:
        o = old_by.get(s.name)
        if o is None:
            issues.append(f"new sleeve {s.name} requires human approval")
            continue
        if s.type != o.type or s.params != o.params:
            issues.append(f"sleeve {s.name}: type/params changes require human approval")
        if not (b.sleeve_allocation_min - 1e-9 <= s.allocation <= b.sleeve_allocation_max + 1e-9):
            issues.append(f"sleeve {s.name}: allocation {s.allocation:.2f} outside [{b.sleeve_allocation_min}, {b.sleeve_allocation_max}]")
        if abs(s.allocation - o.allocation) > b.max_allocation_change_per_week + 1e-9:
            issues.append(f"sleeve {s.name}: change {s.allocation - o.allocation:+.2f} exceeds weekly max {b.max_allocation_change_per_week}")
    if set(old_by) - {s.name for s in new.sleeves}:
        issues.append("removing a sleeve requires human approval")
    if new.vol_target is not None and not (b.vol_target_min - 1e-9 <= new.vol_target <= b.vol_target_max + 1e-9):
        issues.append(f"vol_target {new.vol_target} outside [{b.vol_target_min}, {b.vol_target_max}]")
    if new.risk != old.risk or new.mode != old.mode or new.rebalance != old.rebalance:
        issues.append("risk limits, rebalance params and mode require human approval")
    return issues
