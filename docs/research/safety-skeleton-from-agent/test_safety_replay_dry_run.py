"""Replay test: drives portfolio.safety.run end-to-end in dry-run with recorded targets/prices on a FakeBroker."""
import json
import shutil
from pathlib import Path

from portfolio.safety import run as run_daily

ROOT = Path(__file__).resolve().parents[1]


def _setup(tmp_path):
    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config" / "limits.yaml", tmp_path / "config" / "limits.yaml")
    (tmp_path / "targets.json").write_text(json.dumps({"AAPL": 0.10, "MSFT": 0.10, "GOOG": 0.05}))
    (tmp_path / "prices.json").write_text(json.dumps({"prices": {"AAPL": 100.0, "MSFT": 200.0, "GOOG": 150.0}, "last_bar": "2026-09-08"}))
    return ["--mode", "dry-run", "--date", "2026-09-08", "--root", str(tmp_path), "--ignore-window",
            "--targets-json", str(tmp_path / "targets.json"), "--prices-json", str(tmp_path / "prices.json")]


def test_dry_run_end_to_end_and_idempotent_rerun(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    args = _setup(tmp_path)
    assert run_daily.main(args) == 0
    audit = (tmp_path / "logs" / "audit-202609.jsonl").read_text()
    assert audit.count('"order.dry_run"') == 3 and '"broker.fake"' in audit
    plan = json.loads((tmp_path / "state" / "plans" / "2026-09-08.json").read_text())
    assert {o["symbol"]: o["qty"] for o in plan["orders"]} == {"AAPL": 50, "MSFT": 25, "GOOG": 33}
    state = json.loads((tmp_path / "state" / "runs" / "2026-09-08.json").read_text())
    assert state["phase"] == "done" and state["plan_hash"] == plan["hash"]
    assert run_daily.main(args) == 0  # second run: already DONE, no new plan, no new orders
    assert (tmp_path / "logs" / "audit-202609.jsonl").read_text().count('"order.dry_run"') == 3
    assert (tmp_path / "state" / "heartbeat.json").exists()


def test_holiday_is_skipped_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    args = _setup(tmp_path)
    args[args.index("--date") + 1] = "2026-09-06"  # Sunday
    assert run_daily.main(args) == 0
    audit = (tmp_path / "logs" / "audit-202609.jsonl").read_text()
    assert '"run.skipped"' in audit and '"order.dry_run"' not in audit
