import json
import os
import time

import pytest

from portfolio import state as st


def test_run_lock_exclusive_and_stale(tmp_path):
    lock = tmp_path / "run_lock"
    with st.run_lock(lock):
        assert lock.exists()
        with pytest.raises(st.LockHeld):
            with st.run_lock(lock):
                pass
    assert not lock.exists()
    lock.write_text(json.dumps({"pid": 1, "started_at": "x"}))
    old = time.time() - 10_000
    os.utime(lock, (old, old))
    with st.run_lock(lock, stale_after=3600):
        assert json.loads(lock.read_text())["pid"] == os.getpid()


def test_kill_switch_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "AUDIT_FILE", tmp_path / "audit.jsonl")
    ks_path = tmp_path / "ks.json"
    assert st.read_kill_switch(ks_path).active is False
    ks = st.set_kill_switch(True, "drawdown breach", "engine", path=ks_path)
    assert ks.active and st.read_kill_switch(ks_path).reason == "drawdown breach"
    recs = st.read_audit(tmp_path / "audit.jsonl", event="kill_switch")
    assert recs and recs[-1]["active"] is True


def test_audit_and_read(tmp_path):
    p = tmp_path / "audit.jsonl"
    st.audit("order", path=p, symbol="SPY", notional=100.0, nan=float("nan"))
    st.audit("run", path=p, ok=True)
    recs = st.read_audit(p)
    assert [r["event"] for r in recs] == ["order", "run"]
    assert recs[0]["nan"] is None
    assert st.read_audit(p, last_n=1)[0]["event"] == "run"
    assert st.read_audit(tmp_path / "missing.jsonl") == []


def test_equity_history_idempotent(tmp_path):
    p = tmp_path / "eq.csv"
    st.append_equity("2026-09-08", 100000.0, 5000.0, p)
    st.append_equity("2026-09-09", 101000.0, 4000.0, p)
    st.append_equity("2026-09-08", 100500.0, 5000.0, p)
    rows = st.read_equity_history(p)
    assert rows == [("2026-09-08", 100500.0, 5000.0), ("2026-09-09", 101000.0, 4000.0)]
    assert st.read_equity_history(tmp_path / "none.csv") == []


def test_write_read_json_handles_dataclasses(tmp_path):
    p = tmp_path / "x.json"
    st.write_json(p, {"ks": st.KillSwitch(active=True, reason="r")})
    assert st.read_json(p)["ks"]["active"] is True
    assert st.read_json(tmp_path / "nope.json", default=[]) == []
    st.write_last_run({"a": 1}, path=p)
    assert st.read_last_run(p)["a"] == 1
