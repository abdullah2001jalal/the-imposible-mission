"""SENTINEL v2 — deterministic strategy core (pure Python, no MT5 imports).

Design principles:
- Signals are DETERMINISTIC and testable; no LLM in the trading path.
- Trend filter (EMA50/EMA200) + Donchian breakout entries on closed bars.
- Risk sized as a fixed fraction of equity, ATR-based stops.
- Weekly-cadence position management: breakeven move, regime-flip exit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence


# --------------------------------------------------------------------------- #
# indicators
# --------------------------------------------------------------------------- #
def ema(values: Sequence[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("ema period must be > 0")
    if not values:
        return []
    out = [0.0] * len(values)
    k = 2.0 / (period + 1.0)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1.0 - k)
    return out


def atr(bars: Sequence["Bar"], period: int) -> list[float]:
    """Wilder's ATR."""
    if len(bars) < period + 1:
        return []
    trs = [bars[i].high - bars[i].low for i in range(len(bars))]
    trs[0] = bars[0].high - bars[0].low
    for i in range(1, len(bars)):
        pc = bars[i - 1].close
        trs[i] = max(bars[i].high - bars[i].low, abs(bars[i].high - pc), abs(bars[i].low - pc))
    out = [0.0] * len(bars)
    out[period] = sum(trs[1 : period + 1]) / period
    for i in range(period + 1, len(bars)):
        out[i] = (out[i - 1] * (period - 1) + trs[i]) / period
    return out


def donchian(bars: Sequence["Bar"], lookback: int, use_high: bool) -> float:
    """Extreme of the PREVIOUS `lookback` closed bars (excludes the newest bar's extreme)."""
    seq = bars[len(bars) - lookback - 1 : len(bars) - 1]
    if not seq:
        return 0.0
    vals = [b.high for b in seq] if use_high else [b.low for b in seq]
    return max(vals) if use_high else min(vals)


@dataclass
class Bar:
    time: int  # unix seconds (bars are CLOSED candles)
    open: float
    high: float
    low: float
    close: float


@dataclass
class PositionCtx:
    """Everything needed to manage an existing position."""
    ticket: int
    pair: str
    direction: str          # BUY / SELL
    entry: float
    stop_points: float      # initial risk distance in points
    volume: float
    be_moved: bool = False


@dataclass
class TradePlan:
    pair: str
    action: str             # BUY / SELL / NONE / CLOSE / BE
    stop_points: float = 0.0
    reason: str = ""
    risk_pct: float = 0.0
    extras: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# signals
# --------------------------------------------------------------------------- #
def regime(closes: Sequence[float], fast: int, slow: int) -> int:
    if len(closes) < slow + 2:
        return 0
    e_fast = ema(closes, fast)[-1]
    e_slow = ema(closes, slow)[-1]
    return 1 if e_fast > e_slow else -1 if e_fast < e_slow else 0


def entry_signal(bars: Sequence[Bar], cfg: dict) -> tuple[str, float]:
    """BUY/SELL + stop distance in price... returns (action, atr_value). None-safe."""
    a = atr(bars, int(cfg["atr_period"]))
    fast_n, slow_n = int(cfg["trend_ema_fast"]), int(cfg["trend_ema_slow"])
    if not a or len(bars) < slow_n + int(cfg["breakout_lookback"]) + 2:
        return "NONE", 0.0
    atr_now = a[-1]
    if atr_now <= 0:
        return "NONE", atr_now
    r = regime([b.close for b in bars], fast_n, slow_n)
    look = int(cfg["breakout_lookback"])
    px = bars[-1].close
    if r > 0 and px > donchian(bars, look, use_high=True):
        return "BUY", atr_now * float(cfg["sl_atr"])
    if r < 0 and px < donchian(bars, look, use_high=False):
        return "SELL", atr_now * float(cfg["sl_atr"])
    return "NONE", atr_now


def manage_position(pos: PositionCtx, bars: Sequence[Bar], cfg: dict) -> TradePlan:
    """Rules that run for any OPEN position every cycle."""
    plan = TradePlan(pos.pair, "HOLD")
    a = atr(bars, int(cfg["atr_period"]))
    if not a:
        plan.action = "HOLD"
        return plan
    r = regime([b.close for b in bars], int(cfg["trend_ema_fast"]), int(cfg["trend_ema_slow"]))
    price = bars[-1].close
    stop_dist = pos.stop_points
    if stop_dist <= 0:
        plan.action = "HOLD"
        plan.reason = "no-stop-distance-recorded"
        return plan

    if pos.direction == "BUY":
        r_mult = (price - pos.entry) / stop_dist
        reg_against = r < 0
    else:
        r_mult = (pos.entry - price) / stop_dist
        reg_against = r > 0

    plan.extras["r_multiple"] = round(r_mult, 2)
    if reg_against and bool(cfg.get("exit_flip_trend", True)):
        plan.action = "CLOSE"
        plan.reason = "trend regime flipped against position"
        return plan
    if (not pos.be_moved) and r_mult >= float(cfg["be_at_r"]):
        plan.action = "BE"
        plan.reason = f"breakeven at +{r_mult:.2f}R"
        return plan
    plan.action = "HOLD"
    plan.reason = f"R={r_mult:.2f}"
    return plan
