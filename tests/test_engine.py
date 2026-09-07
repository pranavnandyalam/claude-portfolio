import numpy as np
import pandas as pd
import pytest

from portfolio import config as c
from portfolio import engine as e
from portfolio import state as st
from portfolio.broker import FakeBroker
from portfolio.data import PriceData

NOW = pd.Timestamp("2026-09-08 10:30", tz="America/New_York")


def _loader_for(close):
    return lambda syms, start: PriceData(close=close, volume=close * 0, source="test", fetched_at=pd.Timestamp("2026-09-05").to_pydatetime())

CFG = {
    "name": "t", "mode": "paper", "cash_symbol": "BIL", "vol_target": None,
    "sleeves": [
        {"name": "rot", "type": "rotation", "allocation": 0.5, "params": {"universe": ["UP", "UP2", "DOWN"], "top_n": 2, "cash": "BIL", "min_history_days": 100, "horizons": [20, 40, 60], "sma_days": 50}},
        {"name": "trend", "type": "trend", "allocation": 0.3, "params": {"assets": ["UP", "DOWN"], "cash": "BIL", "sma_days": 50, "min_history_days": 60}},
        {"name": "static", "type": "static", "allocation": 0.2, "params": {"weights": {"AGG": 1.0}}},
    ],
    "risk": {"max_position_weight": 0.6, "max_turnover_pct": 0.3}, "rebalance": {"drift_band_abs": 0.01},
}


def _prices(n=300, end="2026-09-04"):
    idx = pd.bdate_range(end=end, periods=n)
    rng = np.random.default_rng(11)
    def s(drift, vol): return 100 * np.cumprod(1 + rng.normal(drift, vol, n))
    close = pd.DataFrame({"UP": s(0.003, 0.01), "UP2": s(0.002, 0.015), "DOWN": s(-0.003, 0.01), "BIL": s(0.0001, 0.0003), "AGG": s(0.0002, 0.003), "SPY": s(0.001, 0.01)}, index=idx)
    return close


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "AUDIT_FILE", tmp_path / "audit.jsonl")
    monkeypatch.setattr(st, "KILL_SWITCH_FILE", tmp_path / "ks.json")
    monkeypatch.setattr(st, "LAST_RUN_FILE", tmp_path / "last.json")
    monkeypatch.setattr(st, "EQUITY_HISTORY_FILE", tmp_path / "eq.csv")
    monkeypatch.setattr(st, "TARGETS_FILE", tmp_path / "targets.json")
    monkeypatch.setattr(st, "ACCOUNT_FILE", tmp_path / "account.json")
    close = _prices()
    loader = lambda syms, start: PriceData(close=close, volume=close * 0, source="test", fetched_at=pd.Timestamp("2026-09-05").to_pydatetime())
    return close, loader


def test_dry_run_then_execute_reaches_targets(env):
    close, loader = env
    cfg = c.from_dict(CFG)
    last = {s: float(close[s].iloc[-1]) for s in close.columns}
    fb = FakeBroker(cash=50_000, prices=last)
    r = e.run(cfg, fb, execute=False, today=pd.Timestamp("2026-09-08"), price_loader=loader)
    assert r.mode == "dry-run" and r.error == "" and fb.submitted == []
    assert abs(sum(r.targets.values()) - 1) < 1e-6 and r.targets["AGG"] == pytest.approx(0.2)
    assert "DOWN" not in r.targets and r.plan is not None and r.plan.trades
    assert st.read_last_run()["mode"] == "dry-run"
    r2 = e.run(cfg, fb, execute=True, today=pd.Timestamp("2026-09-08"), price_loader=loader, sleep=lambda s: None, now_et=NOW)
    assert r2.mode == "execute" and r2.execution.ok and r2.reconcile_issues == ()
    got = {p.symbol: p.market_value / fb.get_account().equity for p in fb.get_positions()}
    for sym, w in r2.risk.targets.items():
        assert got[sym] == pytest.approx(w * (1 - cfg.rebalance.cash_buffer_pct), abs=0.005)
    r3 = e.run(cfg, fb, execute=True, today=pd.Timestamp("2026-09-08"), price_loader=loader, sleep=lambda s: None, now_et=NOW)
    assert r3.plan.is_empty or all(o.already_existed for o in r3.execution.submitted)
    events = [x["event"] for x in st.read_audit()]
    assert "engine_start" in events and "order_plan" in events and "engine_end" in events


def test_stale_data_is_an_error(env):
    close, loader = env
    cfg = c.from_dict(CFG)
    fb = FakeBroker(cash=50_000, prices={s: float(close[s].iloc[-1]) for s in close.columns})
    r = e.run(cfg, fb, execute=True, today=pd.Timestamp("2026-10-01"), price_loader=loader)
    assert r.mode == "error" and r.error.startswith("data") and fb.submitted == []


def test_kill_switch_halts_and_circuit_breaker_sets_it(env):
    close, loader = env
    cfg = c.from_dict(CFG)
    last = {s: float(close[s].iloc[-1]) for s in close.columns}
    st.set_kill_switch(True, "manual", "test")
    fb = FakeBroker(cash=50_000, prices=last)
    r = e.run(cfg, fb, execute=True, today=pd.Timestamp("2026-09-08"), price_loader=loader)
    assert r.mode == "halted" and fb.submitted == [] and any("kill switch" in h for h in r.risk.halts)
    st.set_kill_switch(False, "reset", "test")
    st.append_equity("2026-09-01", 100_000, 0)  # peak 100k, now 50k → 50% drawdown
    r2 = e.run(cfg, fb, execute=True, today=pd.Timestamp("2026-09-08"), price_loader=loader, sleep=lambda s: None)
    assert r2.risk.circuit_breaker and st.read_kill_switch().active
    assert r2.risk.targets == {"BIL": 1.0}
    assert any("kill switch SET" in n for n in r2.notes)


def test_paper_config_refuses_live_broker(env):
    close, loader = env
    cfg = c.from_dict(CFG)
    fb = FakeBroker(cash=50_000, prices={s: float(close[s].iloc[-1]) for s in close.columns}, is_paper=False)
    r = e.run(cfg, fb, execute=True, today=pd.Timestamp("2026-09-08"), price_loader=loader)
    assert r.mode == "error" and "live" in r.error and fb.submitted == []


def test_vol_target_scales_down(env):
    close, loader = env
    cfg = c.from_dict({**CFG, "vol_target": 0.02})
    fb = FakeBroker(cash=50_000, prices={s: float(close[s].iloc[-1]) for s in close.columns})
    r = e.run(cfg, fb, execute=False, today=pd.Timestamp("2026-09-08"), price_loader=loader)
    assert r.vol_scalar < 1.0 and r.targets["BIL"] > 0.3


def test_market_closed_skips_execution(env):
    close, loader = env
    cfg = c.from_dict(CFG)
    fb = FakeBroker(cash=50_000, prices={s: float(close[s].iloc[-1]) for s in close.columns}, market_open=False)
    r = e.run(cfg, fb, execute=True, today=pd.Timestamp("2026-09-08"), price_loader=loader)
    assert r.mode == "execute" and r.execution.skipped_reason.startswith("market closed") and fb.submitted == []


def test_targets_are_stored_per_period_and_reused(env):
    close, loader = env
    cfg = c.from_dict(CFG)
    fb = FakeBroker(cash=50_000, prices={s: float(close[s].iloc[-1]) for s in close.columns})
    r1 = e.run(cfg, fb, execute=True, today=pd.Timestamp("2026-09-08"), price_loader=loader, sleep=lambda s: None, now_et=NOW)
    assert st.read_targets()["period_key"] == "2026-09" and any("rebalance_day=True" in n for n in r1.notes)
    assert st.read_targets()["executed_on"] == "20260908"
    # a later day in the same month: stored targets reused, wide band → no trades
    c2 = _prices(end="2026-09-14")
    fb.set_prices({s: float(c2[s].iloc[-1]) for s in c2.columns})
    r2 = e.run(cfg, fb, execute=True, today=pd.Timestamp("2026-09-15"), price_loader=_loader_for(c2), sleep=lambda s: None, now_et=NOW)
    assert r2.error == "" and any("using stored targets" in n for n in r2.notes) and r2.targets == r1.targets
    assert all(abs(t.current_weight - t.target_weight) > 0.05 for t in r2.plan.trades)  # only large drift trades
    # hand-edit stored targets to simulate a wildly different mid-period target → blocked by turnover cap
    st.write_targets("2026-09-14", "2026-09", {"BIL": 1.0}, {}, {"executed_on": "20260908"})
    r3 = e.run(cfg, fb, execute=True, today=pd.Timestamp("2026-09-15"), price_loader=_loader_for(c2), sleep=lambda s: None, now_et=NOW)
    assert r3.plan.blocked_reason.startswith("mid-period turnover") and r3.execution.skipped_reason
    # new month → recompute
    c3 = _prices(end="2026-09-30")
    fb.set_prices({s: float(c3[s].iloc[-1]) for s in c3.columns})
    r4 = e.run(cfg, fb, execute=False, today=pd.Timestamp("2026-10-01"), price_loader=_loader_for(c3))
    assert r4.error == "" and st.read_targets()["period_key"] == "2026-10"


def test_account_pin_and_price_disagreement(env):
    close, loader = env
    cfg = c.from_dict(CFG)
    fb = FakeBroker(cash=50_000, prices={s: float(close[s].iloc[-1]) for s in close.columns}, account_id="acct-1")
    r1 = e.run(cfg, fb, execute=False, today=pd.Timestamp("2026-09-08"), price_loader=loader)
    assert r1.error == "" and st.read_json(st.ACCOUNT_FILE)["account_id"] == "acct-1"
    st.write_json(st.ACCOUNT_FILE, {"account_id": "other", "is_paper": True})
    r2 = e.run(cfg, fb, execute=False, today=pd.Timestamp("2026-09-08"), price_loader=loader)
    assert r2.mode == "error" and "account changed" in r2.error
    st.write_json(st.ACCOUNT_FILE, {"account_id": "acct-1", "is_paper": True})
    fb.set_prices({"UP": float(close["UP"].iloc[-1]) * 2})
    r3 = e.run(cfg, fb, execute=True, today=pd.Timestamp("2026-09-08"), price_loader=loader, now_et=NOW)
    assert r3.mode == "halted" and any("price disagreement" in h for h in r3.risk.halts) and fb.submitted == []


def test_period_key():
    assert e.period_key(pd.Timestamp("2026-09-08"), "M") == "2026-09"
    assert e.period_key(pd.Timestamp("2026-09-08"), "W") == "2026-W37"
    assert e.period_key(pd.Timestamp("2026-09-08"), "D") == "2026-09-08"


def test_dry_run_then_execute_same_day_is_still_rebalance_day(env):
    close, loader = env
    cfg = c.from_dict(CFG)
    fb = FakeBroker(cash=50_000, prices={s: float(close[s].iloc[-1]) for s in close.columns})
    r1 = e.run(cfg, fb, execute=False, today=pd.Timestamp("2026-09-08"), price_loader=loader)
    assert r1.mode == "dry-run" and st.read_targets().get("executed_on") is None
    # market closed at first attempt → still not executed
    fb._market_open = False
    r2 = e.run(cfg, fb, execute=True, today=pd.Timestamp("2026-09-09"), price_loader=loader, now_et=NOW)
    assert r2.execution.skipped_reason.startswith("market closed") and st.read_targets().get("executed_on") is None
    fb._market_open = True
    r3 = e.run(cfg, fb, execute=True, today=pd.Timestamp("2026-09-09"), price_loader=loader, sleep=lambda s: None, now_et=NOW)
    assert any("rebalance_day=True" in n for n in r3.notes) and r3.execution.ok and st.read_targets()["executed_on"] == "20260909"
    c4 = _prices(end="2026-09-09")
    fb.set_prices({s: float(c4[s].iloc[-1]) for s in c4.columns})
    r4 = e.run(cfg, fb, execute=True, today=pd.Timestamp("2026-09-10"), price_loader=_loader_for(c4), sleep=lambda s: None, now_et=NOW)
    assert r4.error == "" and any("rebalance_day=False" in n for n in r4.notes)
