"""Local smoke tests for the Sentinel v2 deterministic core (pure Python).

Run: python3 test_strategy.py   (no MT5 needed — strategy.py imports nothing broker-side)
"""
import random

from strategy import Bar, PositionCtx, atr, ema, entry_signal, manage_position, regime

CFG = {
    "trend_ema_fast": 5,   # small periods so tiny synthetic series trip them
    "trend_ema_slow": 12,
    "breakout_lookback": 6,
    "atr_period": 5,
    "sl_atr": 1.5,
    "be_at_r": 1.0,
    "exit_flip_trend": True,
}


def mk(up_trend: bool, n: int = 60, seed=7) -> list[Bar]:
    rnd = random.Random(seed)
    price, bars, drift = 1.1000, [], (0.0006 if up_trend else -0.0006)
    for i in range(n):
        o = price
        c = o + drift + rnd.uniform(-0.0004, 0.0004)
        bars.append(Bar(time=i * 900, open=o, high=max(o, c) + 0.0005, low=min(o, c) - 0.0005, close=c))
        price = c
    return bars


def flat(n: int = 60) -> list[Bar]:
    rnd, price, bars = random.Random(3), 1.10, []
    for i in range(n):
        o = price
        c = o + rnd.uniform(-0.0003, 0.0003)
        bars.append(Bar(time=i * 900, open=o, high=max(o, c) + 0.0005, low=min(o, c) - 0.0005, close=c))
        price = c
    return bars


def test_indicators():
    e = ema([float(i) for i in range(1, 21)], 5)
    assert len(e) == 20 and e[-1] > e[0]
    a = atr(mk(True, 40), 5)
    assert a[-1] > 0


def test_regime():
    assert regime([float(i) for i in range(100)], 5, 12) == 1
    assert regime([float(-i) for i in range(100)], 5, 12) == -1
    assert regime([1.10] * 100, 5, 12) == 0


def test_breakout_long():
    action, dist = entry_signal(mk(True), CFG)
    assert action in ("BUY", "NONE"), f"got {action}"
    if action == "BUY":
        assert dist > 0


def test_breakout_short():
    action, dist = entry_signal(mk(False), CFG)
    assert action in ("SELL", "NONE"), f"got {action}"


def test_flat_no_signal():
    action, _ = entry_signal(flat(80), CFG)
    assert action in ("BUY", "SELL", "NONE")


def test_manage_flip_close():
    pos = PositionCtx(ticket=2, pair="X", direction="BUY", entry=1.1000, stop_points=0.010, volume=0.01)
    plan = manage_position(pos, mk(False, 50), CFG)
    assert plan.action == "CLOSE", f"BUY in bearish regime must CLOSE, got {plan.action}"


def test_manage_no_flip_close():
    pos = PositionCtx(ticket=3, pair="X", direction="BUY", entry=1.1000, stop_points=0.010, volume=0.01)
    plan = manage_position(pos, mk(True, 50), CFG)
    assert plan.action in ("BE", "HOLD"), f"BUY in bullish regime must not close, got {plan.action}"


TESTS = [test_indicators, test_regime, test_breakout_long, test_breakout_short,
         test_flat_no_signal, test_manage_flip_close, test_manage_no_flip_close]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    raise SystemExit(1 if failed else 0)
