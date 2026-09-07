from datetime import date

import pytest

from portfolio.safety.ids import client_order_id, coid_prefix

D = date(2026, 9, 8)


def test_coid_is_deterministic_and_bounded():
    a = client_order_id(D, "AAPL", "buy", 10, "v1")
    assert a == client_order_id(D, "AAPL", "buy", 10, "v1")
    assert a.startswith(coid_prefix(D, "AAPL")) and len(a) <= 128


def test_coid_changes_with_qty_side_version_date():
    base = client_order_id(D, "AAPL", "buy", 10, "v1")
    assert base != client_order_id(D, "AAPL", "buy", 11, "v1")
    assert base != client_order_id(D, "AAPL", "sell", 10, "v1")
    assert base != client_order_id(D, "AAPL", "buy", 10, "v2")
    assert base != client_order_id(date(2026, 9, 9), "AAPL", "buy", 10, "v1")


@pytest.mark.parametrize("qty", [0, -1, 1.5])
def test_coid_rejects_bad_qty(qty):
    with pytest.raises(ValueError):
        client_order_id(D, "AAPL", "buy", qty, "v1")
