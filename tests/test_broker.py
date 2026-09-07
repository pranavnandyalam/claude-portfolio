import os
from pathlib import Path

import pytest

from portfolio import broker as b


def test_order_request_validation():
    with pytest.raises(ValueError):
        b.OrderRequest(symbol="SPY", side="hold", notional=10)
    with pytest.raises(ValueError):
        b.OrderRequest(symbol="SPY", side="buy")
    with pytest.raises(ValueError):
        b.OrderRequest(symbol="SPY", side="buy", notional=10, qty=1)
    with pytest.raises(ValueError):
        b.OrderRequest(symbol="SPY", side="buy", notional=0.5)
    with pytest.raises(ValueError):
        b.OrderRequest(symbol="SPY", side="sell", qty=0)
    assert b.OrderRequest(symbol="SPY", side="buy", notional=100).notional == 100


def test_fake_broker_fills_and_is_idempotent():
    fb = b.FakeBroker(cash=1000, prices={"SPY": 100.0})
    o1 = fb.submit_order(b.OrderRequest(symbol="SPY", side="buy", notional=500, client_order_id="cid-1"))
    assert o1.status == "filled" and o1.filled_qty == 5 and not o1.already_existed
    o2 = fb.submit_order(b.OrderRequest(symbol="SPY", side="buy", notional=500, client_order_id="cid-1"))
    assert o2.already_existed and o2.id == o1.id
    acct = fb.get_account()
    assert acct.cash == 500 and acct.equity == 1000
    assert fb.get_positions()[0].qty == 5
    with pytest.raises(b.BrokerError):
        fb.submit_order(b.OrderRequest(symbol="SPY", side="buy", notional=600, client_order_id="cid-2"))
    fb.set_prices({"SPY": 120.0})
    assert fb.get_account().equity == 500 + 600
    closed = fb.close_position("SPY")
    assert closed is not None and closed.side == "sell" and fb.get_positions() == []
    assert fb.close_position("SPY") is None
    assert fb.get_order_by_client_id("nope") is None
    with pytest.raises(b.BrokerError):
        fb.latest_price("QQQ")


def test_fake_broker_non_fractionable_rounds_down():
    fb = b.FakeBroker(cash=1000, prices={"BRK": 300.0}, fractionable=set())
    o = fb.submit_order(b.OrderRequest(symbol="BRK", side="buy", notional=1000))
    assert o.filled_qty == 3
    assert not fb.is_fractionable("BRK")


def test_load_keys_from_env_then_files(monkeypatch, tmp_path):
    for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(b, "KEY_FILES", (tmp_path / "nope.env",))
    with pytest.raises(b.AuthError):
        b.load_alpaca_keys()
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nexport ALPACA_API_KEY='k1'\nALPACA_SECRET_KEY=\"s1\"\n")
    monkeypatch.setattr(b, "KEY_FILES", (env_file,))
    assert b.load_alpaca_keys() == ("k1", "s1")
    monkeypatch.setenv("APCA_API_KEY_ID", "k2")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s2")
    assert b.load_alpaca_keys() == ("k2", "s2")


def test_live_gate_is_closed_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(b.LIVE_ENV_FLAG, raising=False)
    monkeypatch.setattr(b, "LIVE_CONFIRM_FILE", tmp_path / "LIVE_CONFIRMED")
    assert not b.live_gate_open()
    with pytest.raises(b.LiveTradingBlocked):
        b.AlpacaBroker(live=True)
    monkeypatch.setenv(b.LIVE_ENV_FLAG, "1")
    assert not b.live_gate_open()
    (tmp_path / "LIVE_CONFIRMED").write_text("nope")
    assert not b.live_gate_open()
    (tmp_path / "LIVE_CONFIRMED").write_text(b.LIVE_CONFIRM_PHRASE + "\n")
    assert b.live_gate_open()


def test_client_order_id_deterministic():
    a = b.make_client_order_id("20260908", "SPY", "buy")
    assert a == b.make_client_order_id("20260908", "SPY", "buy")
    assert a != b.make_client_order_id("20260908", "SPY", "sell")
    assert len(b.make_client_order_id("20260908", "X" * 60, "buy")) <= 48
    assert b.round_qty(1.23456789) == 1.234567


def test_wrap_maps_401_to_auth_error():
    class E(Exception):
        status_code = 401

    assert isinstance(b.AlpacaBroker._wrap(E("unauthorized")), b.AuthError)
    assert isinstance(b.AlpacaBroker._wrap(RuntimeError("boom")), b.BrokerError)
