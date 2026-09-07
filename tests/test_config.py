import pytest
import yaml

from portfolio import config as c

GOOD = {
    "name": "t", "mode": "paper", "cash_symbol": "bil", "vol_target": 0.10,
    "sleeves": [
        {"name": "rot", "type": "rotation", "allocation": 0.5, "params": {"universe": ["SPY", "TLT"], "top_n": 1}},
        {"name": "trend", "type": "trend", "allocation": 0.3, "params": {"assets": ["SPY", "IEF"], "cash": "BIL"}},
        {"name": "gem", "type": "gem", "allocation": 0.2, "params": {"us": "SPY", "intl": "VEU", "bonds": "AGG", "tbill": "BIL"}},
    ],
    "risk": {"max_position_weight": 0.5}, "rebalance": {"drift_band_abs": 0.03}, "adapt": {}, "schedule": {"rebalance_freq": "M"},
}


def test_load_good_config(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text(yaml.safe_dump(GOOD))
    cfg = c.load_config(p)
    assert cfg.cash_symbol == "BIL" and cfg.vol_target == 0.10 and cfg.risk.max_position_weight == 0.5
    assert cfg.rebalance.drift_band_abs == 0.03 and len(cfg.enabled_sleeves) == 3
    assert set(cfg.all_symbols) >= {"BIL", "SPY", "TLT", "IEF", "VEU", "AGG"}


def test_validation_errors(tmp_path):
    with pytest.raises(c.ConfigError):
        c.from_dict({**GOOD, "mode": "yolo"})
    with pytest.raises(c.ConfigError):
        c.from_dict({**GOOD, "sleeves": []})
    bad = [dict(GOOD["sleeves"][0]), dict(GOOD["sleeves"][0])]
    with pytest.raises(c.ConfigError):
        c.from_dict({**GOOD, "sleeves": bad})
    with pytest.raises(c.ConfigError):
        c.from_dict({**GOOD, "sleeves": [{**GOOD["sleeves"][0], "type": "magic"}]})
    with pytest.raises(c.ConfigError):
        c.from_dict({**GOOD, "sleeves": [{**GOOD["sleeves"][0], "allocation": 0.9}, {**GOOD["sleeves"][1], "allocation": 0.5}]})
    with pytest.raises(c.ConfigError):
        c.from_dict({**GOOD, "risk": {"nope": 1}})
    with pytest.raises(c.ConfigError):
        c.from_dict({**GOOD, "vol_target": 0.9})
    with pytest.raises(c.ConfigError):
        c.load_config(tmp_path / "missing.yaml")
    (tmp_path / "bad.yaml").write_text("a: [")
    with pytest.raises(c.ConfigError):
        c.load_config(tmp_path / "bad.yaml")


def test_bounded_adaptation():
    cfg = c.from_dict(GOOD)
    ok = c.with_allocations(cfg, {"rot": 0.55, "trend": 0.25})
    assert ok.sleeves[0].allocation == 0.55 and cfg.sleeves[0].allocation == 0.5  # immutable
    assert c.check_adaptation(cfg, ok) == []
    too_big = c.with_allocations(cfg, {"rot": 0.2, "trend": 0.6})
    issues = c.check_adaptation(cfg, too_big)
    assert any("exceeds weekly max" in i for i in issues)
    with pytest.raises(c.ConfigError):
        c.with_allocations(cfg, {"nope": 0.1})
    with pytest.raises(c.ConfigError):
        c.with_allocations(cfg, {"rot": 0.9})  # sum > 1
    dumped = yaml.safe_load(c.dump_yaml(ok))
    assert dumped["sleeves"][0]["allocation"] == 0.55 and dumped["mode"] == "paper"
    assert c.from_dict(dumped).sleeves[0].allocation == 0.55
    from dataclasses import replace
    changed_mode = replace(cfg, mode="live")
    assert any("human approval" in i for i in c.check_adaptation(cfg, changed_mode))
    fewer = replace(cfg, sleeves=cfg.sleeves[:2])
    assert any("removing" in i for i in c.check_adaptation(cfg, fewer))
    vt = replace(cfg, vol_target=0.25)
    assert any("vol_target" in i for i in c.check_adaptation(cfg, vt))
