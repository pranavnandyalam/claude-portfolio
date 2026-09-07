from portfolio.broker import Account
from portfolio.risk import RiskLimits, apply_position_limits, evaluate, peak_equity


def acct(equity=100_000.0, last=100_000.0, status="ACTIVE"):
    return Account(equity=equity, cash=equity, buying_power=equity, last_equity=last, status=status, is_paper=True)


def test_peak_equity():
    assert peak_equity([("d1", 90.0, 0), ("d2", 120.0, 0)], 100.0) == 120.0
    assert peak_equity([], 100.0) == 100.0


def test_apply_position_limits_clips_and_scales():
    lim = RiskLimits(max_position_weight=0.3, max_gross_exposure=1.0, max_symbols=2)
    t, w = apply_position_limits({"A": 0.5, "B": 0.3, "C": 0.3, "D": -0.1, "E": 0.0}, lim)
    assert set(t) == {"A", "B"} or set(t) == {"A", "C"}
    assert max(t.values()) <= 0.3 + 1e-9
    assert any("clipped" in x for x in w) and any("max 2" in x for x in w)
    t2, w2 = apply_position_limits({"A": 0.7, "B": 0.6}, RiskLimits(max_position_weight=0.7))
    assert abs(sum(t2.values()) - 1.0) < 1e-9 and any("scaled" in x for x in w2)


def test_evaluate_ok_path():
    v = evaluate({"SPY": 0.6, "AGG": 0.4}, acct(), peak=100_000, limits=RiskLimits(max_position_weight=0.6))
    assert v.ok and not v.halts and v.targets == {"SPY": 0.6, "AGG": 0.4}
    assert "ok=True" in v.summary


def test_evaluate_halts():
    lim = RiskLimits()
    v = evaluate({"SPY": 0.5}, acct(equity=96_000, last=100_000), peak=100_000, limits=lim)
    assert not v.ok and any("daily loss" in h for h in v.halts)
    v2 = evaluate({"SPY": 0.5}, acct(), peak=100_000, limits=lim, kill_switch_active=True, data_issues=["stale"])
    assert any("kill switch" in h for h in v2.halts) and any("data: stale" in h for h in v2.halts)
    v3 = evaluate({"SPY": 0.5}, acct(equity=500, last=500), peak=500, limits=lim)
    assert any("below minimum" in h for h in v3.halts)
    v4 = evaluate({"SPY": 0.5}, acct(status="ACCOUNT_CLOSED"), peak=100_000, limits=lim)
    assert any("status" in h for h in v4.halts)


def test_drawdown_warning_and_circuit_breaker():
    lim = RiskLimits(warn_drawdown_pct=0.10, max_drawdown_pct=0.15)
    v = evaluate({"SPY": 0.5}, acct(equity=89_000, last=89_500), peak=100_000, limits=lim)
    assert v.ok and any("drawdown" in w for w in v.warnings) and not v.circuit_breaker
    v2 = evaluate({"SPY": 0.5}, acct(equity=84_000, last=84_500), peak=100_000, limits=lim, risk_off_symbol="BIL")
    assert v2.circuit_breaker and not v2.ok and v2.targets == {"BIL": 1.0}
    v3 = evaluate({"SPY": 0.5}, acct(equity=84_000, last=84_500), peak=100_000, limits=lim)
    assert v3.circuit_breaker and v3.targets == {}
