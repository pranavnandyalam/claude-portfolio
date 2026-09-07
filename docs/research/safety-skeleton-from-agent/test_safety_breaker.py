from datetime import date

from portfolio.safety.breaker import BreakerState, evaluate
from portfolio.safety.config import Limits

L = Limits()
D = date(2026, 9, 8)


def test_levels_escalate_with_drawdown():
    s = BreakerState(peak_equity=100_000)
    s1, d1 = evaluate(s, 94_000, 94_000, D, L)
    assert d1.level == 1 and d1.allow_buys and d1.gross_scale == 0.5
    s2, d2 = evaluate(s1, 89_000, 89_000, D, L)
    assert d2.level == 2 and not d2.allow_buys and d2.allow_sells
    s3, d3 = evaluate(s2, 84_000, 84_000, D, L)
    assert d3.level == 3 and d3.flatten and d3.halt_new_orders


def test_l1_hysteresis():
    s, _ = evaluate(BreakerState(peak_equity=100_000), 94_000, 94_000, D, L)
    s, d = evaluate(s, 96_500, 96_500, D, L)   # dd 3.5% : still L1 (reset at <3%)
    assert d.level == 1
    s, d = evaluate(s, 97_500, 97_500, D, L)   # dd 2.5% : released
    assert d.level == 0 and d.gross_scale == 1.0


def test_l3_is_sticky_until_human_reset_file(tmp_path):
    s, _ = evaluate(BreakerState(peak_equity=100_000), 84_000, 84_000, D, L)
    s, d = evaluate(s, 99_000, 99_000, D, L, reset_file=tmp_path / "BREAKER_RESET")
    assert d.level == 3 and d.halt_new_orders
    (tmp_path / "BREAKER_RESET").write_text("L3 2026-09-09 pranav reviewed losses, restarting at half size\n")
    s, d = evaluate(s, 99_000, 99_000, D, L, reset_file=tmp_path / "BREAKER_RESET")
    assert d.level == 0 and not (tmp_path / "BREAKER_RESET").exists() and s.last_reset_by.startswith("L3 ")
    assert any(p.name.startswith("BREAKER_RESET.consumed-") for p in tmp_path.iterdir())


def test_daily_loss_halt_and_cancel_levels():
    s = BreakerState(peak_equity=100_000)
    _, d = evaluate(s, 96_500, 100_000, D, L)
    assert d.halt_new_orders and not d.cancel_open and not d.allow_buys
    _, d = evaluate(s, 94_000, 100_000, D, L)
    assert d.halt_new_orders and d.cancel_open


def test_peak_ratchets_up():
    s, _ = evaluate(BreakerState(peak_equity=100_000), 120_000, 100_000, D, L)
    assert s.peak_equity == 120_000
    _, d = evaluate(s, 113_000, 120_000, D, L)
    assert d.level == 1  # 5.8% off the new peak
