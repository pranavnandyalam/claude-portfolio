import json

import pytest

from portfolio.safety.lock import AlreadyRunning, IllegalTransition, Phase, RunLock, RunState


def test_second_lock_refused(tmp_path):
    p = tmp_path / "run.lock"
    with RunLock(p):
        with pytest.raises(AlreadyRunning):
            with RunLock(p):
                pass
    assert not p.exists()


def test_stale_lock_from_dead_pid_is_reclaimed(tmp_path):
    p = tmp_path / "run.lock"
    p.write_text(json.dumps({"pid": 2_000_000_000, "started": 0}))
    with RunLock(p):
        assert json.loads(p.read_text())["pid"] != 2_000_000_000


def test_state_machine_rejects_illegal_transitions(tmp_path):
    s = RunState("2026-09-08", "run-20260908-01")
    s = s.advance(Phase.PREFLIGHT).advance(Phase.PLANNED)
    with pytest.raises(IllegalTransition):
        s.advance(Phase.RECONCILED)
    with pytest.raises(IllegalTransition):
        s.advance(Phase.DONE).advance(Phase.PREFLIGHT)


def test_plan_hash_cannot_change_within_run():
    s = RunState("2026-09-08", "r").with_plan("abc")
    with pytest.raises(IllegalTransition):
        s.with_plan("def")


def test_state_roundtrip_and_immutability(tmp_path):
    p = tmp_path / "runs" / "2026-09-08.json"
    s0 = RunState("2026-09-08", "r")
    s1 = s0.advance(Phase.PREFLIGHT).with_submitted("cp-1").with_submitted("cp-1")
    s1.save(p)
    loaded = RunState.load(p)
    assert s0.phase is Phase.INIT and loaded.phase is Phase.PREFLIGHT
    assert loaded.submitted_coids == ("cp-1",) and len(loaded.history) == 1
