from datetime import date

from portfolio.safety.audit import AuditLog
from portfolio.safety.broker import FakeBroker
from portfolio.safety.execute import await_fills, submit_plan
from portfolio.safety.rebalance import Plan, PlannedOrder

D = date(2026, 9, 8)
NOSLEEP = lambda s: None  # noqa: E731


def _plan(*orders):
    return Plan(tuple(orders), (), 100_000.0, 0.01, 1.0)


def _audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl", "run-test")


def test_ambiguous_504_after_accept_does_not_duplicate(tmp_path):
    b = FakeBroker(prices={"AAPL": 100.0})
    b.fail_submit_once = "504-after-accept"
    res = submit_plan(b, _plan(PlannedOrder("AAPL", "buy", 10, 100.0, "t")), D, "v1", _audit(tmp_path), dry_run=False, sleep=NOSLEEP)
    assert len(b.get_orders()) == 1 and len(res.submitted) == 1
    assert [c[0] for c in b.calls].count("submit") == 1  # retry path looked up by coid instead of re-POSTing


def test_504_before_accept_retries_exactly_once_more(tmp_path):
    b = FakeBroker(prices={"AAPL": 100.0})
    b.fail_submit_once = "504-before-accept"
    res = submit_plan(b, _plan(PlannedOrder("AAPL", "buy", 10, 100.0, "t")), D, "v1", _audit(tmp_path), dry_run=False, sleep=NOSLEEP)
    assert len(b.get_orders()) == 1 and len(res.submitted) == 1
    assert [c[0] for c in b.calls].count("submit") == 2


def test_rerun_same_plan_skips_existing(tmp_path):
    b = FakeBroker(prices={"AAPL": 100.0, "MSFT": 200.0})
    plan = _plan(PlannedOrder("AAPL", "buy", 10, 100.0, "t"), PlannedOrder("MSFT", "sell", 5, 200.0, "t"))
    first = submit_plan(b, plan, D, "v1", _audit(tmp_path), dry_run=False, sleep=NOSLEEP)
    second = submit_plan(b, plan, D, "v1", _audit(tmp_path), dry_run=False, sleep=NOSLEEP)
    assert len(first.submitted) == 2 and len(second.submitted) == 0
    assert set(second.skipped_existing) == set(first.coids) and len(b.get_orders()) == 2


def test_same_symbol_different_qty_same_day_is_still_blocked(tmp_path):
    """Prefix guard: a re-planned qty must not produce a second order for the symbol on the same day."""
    b = FakeBroker(prices={"AAPL": 100.0})
    submit_plan(b, _plan(PlannedOrder("AAPL", "buy", 10, 100.0, "t")), D, "v1", _audit(tmp_path), dry_run=False, sleep=NOSLEEP)
    res = submit_plan(b, _plan(PlannedOrder("AAPL", "buy", 12, 100.0, "t")), D, "v1", _audit(tmp_path), dry_run=False, sleep=NOSLEEP)
    assert len(res.submitted) == 0 and len(b.get_orders()) == 1


def test_rejected_order_is_recorded_not_retried(tmp_path):
    b = FakeBroker(prices={"AAPL": 100.0})
    b.reject_symbols = {"AAPL"}
    res = submit_plan(b, _plan(PlannedOrder("AAPL", "buy", 10, 100.0, "t")), D, "v1", _audit(tmp_path), dry_run=False, sleep=NOSLEEP)
    assert len(res.rejected) == 1 and [c[0] for c in b.calls].count("submit") == 1


def test_dry_run_never_touches_broker_submit(tmp_path):
    b = FakeBroker(prices={"AAPL": 100.0})
    res = submit_plan(b, _plan(PlannedOrder("AAPL", "buy", 10, 100.0, "t")), D, "v1", _audit(tmp_path), dry_run=True)
    assert len(res.dry_run) == 1 and not any(c[0] == "submit" for c in b.calls)


def test_partial_fill_is_canceled_at_deadline_and_reported(tmp_path):
    b = FakeBroker(prices={"AAPL": 100.0})
    b.partial_fill_ratio = 0.5
    audit = _audit(tmp_path)
    res = submit_plan(b, _plan(PlannedOrder("AAPL", "buy", 10, 100.0, "t")), D, "v1", audit, dry_run=False, sleep=NOSLEEP)
    b.fill_open_orders()
    t = iter([0.0, 100.0, 200.0])
    fills = await_fills(b, res.coids, audit, wait_s=1.0, sleep=NOSLEEP, clock=lambda: next(t))
    assert fills[0].status == "canceled" and fills[0].filled_qty == 5.0
    events = [l for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any('"order.partial_fill"' in l for l in events) and any('"order.canceled_at_deadline"' in l for l in events)
