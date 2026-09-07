from dataclasses import replace

import pandas as pd
import pytest

from portfolio import executor as ex

NOW = pd.Timestamp("2026-09-08 10:30", tz="America/New_York")
from portfolio.broker import Account, BrokerError, FakeBroker, Position
from portfolio.rebalance import build_order_plan


def _plan(broker: FakeBroker, targets, prices, run_date="20260908"):
    return build_order_plan(targets, broker.get_positions(), broker.get_account(), prices, run_date,
                            client_id_fn=lambda s, side: f"cp-{run_date}-{s}-{side}")


def test_dry_run_submits_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("portfolio.state.AUDIT_FILE", tmp_path / "a.jsonl")
    fb = FakeBroker(cash=10_000, prices={"SPY": 100.0})
    res = ex.execute_plan(fb, _plan(fb, {"SPY": 1.0}, {"SPY": 100.0}), dry_run=True)
    assert res.dry_run and not res.submitted and fb.submitted == []


def test_execute_sells_then_buys_and_reconciles(tmp_path, monkeypatch):
    monkeypatch.setattr("portfolio.state.AUDIT_FILE", tmp_path / "a.jsonl")
    fb = FakeBroker(cash=0.0, prices={"SPY": 100.0, "AGG": 50.0}, positions={"SPY": 1000.0})  # $100k in SPY
    plan = _plan(fb, {"SPY": 0.5, "AGG": 0.5}, {"SPY": 100.0, "AGG": 50.0})
    res = ex.execute_plan(fb, plan, dry_run=False, sleep=lambda s: None, now_et=NOW)
    assert res.ok and res.n_filled == 2
    assert [r.side for r in fb.submitted] == ["sell", "buy"]
    issues = ex.reconcile(fb, {"SPY": 0.5, "AGG": 0.5}, tolerance=0.03)
    assert issues == []
    # re-running the same plan is idempotent at the broker (same client ids)
    res2 = ex.execute_plan(fb, plan, dry_run=False, sleep=lambda s: None, now_et=NOW)
    assert all(o.already_existed for o in res2.submitted)
    late = ex.execute_plan(fb, plan, dry_run=False, sleep=lambda s: None, now_et=pd.Timestamp("2026-09-08 16:10", tz="America/New_York"))
    assert late.skipped_reason.startswith("outside execution window")
    assert len(fb.submitted) == 2


def test_market_closed_skips(tmp_path, monkeypatch):
    monkeypatch.setattr("portfolio.state.AUDIT_FILE", tmp_path / "a.jsonl")
    fb = FakeBroker(cash=10_000, prices={"SPY": 100.0}, market_open=False)
    res = ex.execute_plan(fb, _plan(fb, {"SPY": 1.0}, {"SPY": 100.0}), dry_run=False, sleep=lambda s: None)
    assert res.skipped_reason.startswith("market closed") and not fb.submitted
    res2 = ex.execute_plan(fb, _plan(fb, {"SPY": 1.0}, {"SPY": 100.0}), dry_run=False, require_market_open=False, sleep=lambda s: None)
    assert res2.ok


def test_blocked_and_empty_plans_skip(tmp_path, monkeypatch):
    monkeypatch.setattr("portfolio.state.AUDIT_FILE", tmp_path / "a.jsonl")
    fb = FakeBroker(cash=10_000, prices={"SPY": 100.0})
    plan = _plan(fb, {"SPY": 1.0}, {"SPY": 100.0})
    res = ex.execute_plan(fb, replace(plan, blocked_reason="turnover"), dry_run=False)
    assert res.skipped_reason == "turnover"
    res2 = ex.execute_plan(fb, replace(plan, trades=()), dry_run=False)
    assert res2.skipped_reason == "empty plan"


def test_partial_failures_are_reported(tmp_path, monkeypatch):
    monkeypatch.setattr("portfolio.state.AUDIT_FILE", tmp_path / "a.jsonl")
    fb = FakeBroker(cash=10_000, prices={"SPY": 100.0, "AGG": 50.0})
    plan = _plan(fb, {"SPY": 0.5, "AGG": 0.5}, {"SPY": 100.0, "AGG": 50.0})
    orig = fb.submit_order

    def flaky(req):
        if req.symbol == "AGG":
            raise BrokerError("simulated reject")
        return orig(req)

    fb.submit_order = flaky
    res = ex.execute_plan(fb, plan, dry_run=False, sleep=lambda s: None, now_et=NOW)
    assert not res.ok and res.failed == (("AGG", "simulated reject"),) and res.n_filled == 1


def test_wait_for_terminal_polls_until_filled():
    class B:
        def __init__(self):
            self.calls = 0

        def get_order(self, oid):
            self.calls += 1
            from portfolio.broker import Order
            return Order(id=oid, client_order_id="c", symbol="SPY", side="buy", status="filled" if self.calls >= 2 else "new")

    from portfolio.broker import Order
    pending = Order(id="1", client_order_id="c", symbol="SPY", side="buy", status="new")
    b = B()
    out = ex._wait_for_terminal(b, [pending], timeout=5, poll=0.01, sleep=lambda s: None)
    assert out[0].status == "filled" and b.calls == 2
