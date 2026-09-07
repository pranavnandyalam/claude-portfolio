import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from portfolio.safety.audit import AuditLog
from portfolio.safety.broker import AccountSnap, ClockSnap, FakeBroker
from portfolio.safety.checks import (check_account, check_bars_fresh, check_is_trading_day, check_price_agreement,
                                     check_prices_finite, check_trading_window, last_session_on_or_before, summarize)
from portfolio.safety.gate import (LIVE_ENV, LIVE_ENV_VALUE, LIVE_FILE, LIVE_URL, PAPER_URL, GateRefused, Mode,
                                   assert_account_matches, resolve_mode)

LABOR_DAY = date(2026, 9, 7)


def _sessions():
    b = FakeBroker()
    b.holidays = {LABOR_DAY}
    return b.get_calendar(date(2026, 8, 28), date(2026, 9, 9))


def test_staleness_uses_calendar_not_wall_clock():
    s = _sessions()
    assert last_session_on_or_before(s, LABOR_DAY) == date(2026, 9, 4)
    assert not check_is_trading_day(s, LABOR_DAY).ok and check_is_trading_day(s, date(2026, 9, 8)).ok
    assert check_bars_fresh(date(2026, 9, 4), date(2026, 9, 4)).ok
    assert not check_bars_fresh(date(2026, 9, 3), date(2026, 9, 4)).ok
    assert not check_bars_fresh(None, date(2026, 9, 4)).ok


def test_price_sanity_checks():
    assert not check_prices_finite({"AAPL": 100.0, "X": float("nan"), "Y": 0.0}).ok
    assert check_price_agreement({"AAPL": 100.0}, {"AAPL": 102.0}, 0.03).ok
    assert not check_price_agreement({"AAPL": 100.0}, {"AAPL": 106.0}, 0.03).ok


def test_account_and_window_checks():
    good = AccountSnap("id", "PA1", "ACTIVE", 100.0, 100.0, 100.0, 100.0)
    assert check_account(good).ok
    assert not check_account(AccountSnap("id", "PA1", "ACTIVE", 100.0, 100.0, 100.0, 100.0, trading_blocked=True)).ok
    et = ZoneInfo("America/New_York")
    clock = ClockSnap(datetime.now(timezone.utc), True, datetime.now(timezone.utc), datetime.now(timezone.utc))
    assert check_trading_window(clock, datetime(2026, 9, 8, 10, 0, tzinfo=et)).ok
    assert not check_trading_window(clock, datetime(2026, 9, 8, 15, 50, tzinfo=et)).ok
    ok, _ = summarize([check_account(good), check_bars_fresh(None, None)])
    assert not ok


def test_live_gate_requires_env_and_file(tmp_path):
    assert resolve_mode("paper", tmp_path, env={}) is Mode.PAPER
    with pytest.raises(GateRefused):
        resolve_mode("live", tmp_path, env={})
    with pytest.raises(GateRefused):
        resolve_mode("live", tmp_path, env={LIVE_ENV: LIVE_ENV_VALUE})
    (tmp_path / LIVE_FILE).write_text("acct-live-123\n")
    assert resolve_mode("live", tmp_path, env={LIVE_ENV: LIVE_ENV_VALUE}) is Mode.LIVE


def test_account_url_assertions_and_pin(tmp_path):
    with pytest.raises(GateRefused):
        assert_account_matches(Mode.PAPER, LIVE_URL, "a1", "PA1", tmp_path)
    with pytest.raises(GateRefused):
        assert_account_matches(Mode.PAPER, PAPER_URL, "a1", "123LIVE", tmp_path)
    assert_account_matches(Mode.PAPER, PAPER_URL, "a1", "PA1", tmp_path)
    with pytest.raises(GateRefused):  # key silently swapped to a different paper account
        assert_account_matches(Mode.PAPER, PAPER_URL, "a2", "PA2", tmp_path)
    (tmp_path / LIVE_FILE).write_text("live-1")
    with pytest.raises(GateRefused):
        assert_account_matches(Mode.LIVE, LIVE_URL, "live-2", "123", tmp_path)
    assert_account_matches(Mode.LIVE, LIVE_URL, "live-1", "123", tmp_path)


def test_audit_chain_and_redaction(tmp_path):
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p, "run-1", redactor=lambda s: s.replace("SECRETVALUE1", "***"))
    a.write("run.start", key="SECRETVALUE1")
    a.write("x", n=1)
    AuditLog(p, "run-2").write("y", n=2)  # new writer continues the chain
    assert "SECRETVALUE1" not in p.read_text()
    assert AuditLog.verify_chain(p) == (True, 3)
    lines = p.read_text().splitlines()
    rec = json.loads(lines[1]); rec["payload"]["n"] = 999
    p.write_text("\n".join([lines[0], json.dumps(rec, sort_keys=True), lines[2]]) + "\n")
    assert AuditLog.verify_chain(p)[0] is False
