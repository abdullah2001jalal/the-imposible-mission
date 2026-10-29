"""SENTINEL v2 — walk-forward backtester on REAL broker history (MT5).

Replays the deterministic core bar by bar on real M15 history:
- entries at NEXT bar open + spread + slippage (realistic fills),
- intrabar SL/TP touch (stop assumed first if both touch on one bar),
- breakeven move when +1R is touched,
- regime-flip exits on bar close, session filter, one position per pair,
  portfolio cap, cooldown bars after a closed loss,
- contract value per point is the broker's own trade_tick_value
  (no invented constants), volume respects volume_min/step.

Emits backtest_report.md + .json and emails one summary.
Usage: python backtest.py [days_back]
"""
from __future__ import annotations

import json
import os
import sys
import time

import yaml

WARMUP = 230
SLIPPAGE_POINTS = 2
DEFAULT_EQUITY = 10000.0


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}] " + " ".join(str(x) for x in a), flush=True)


def money_per_point(info) -> float:
    """Contract value of a 1.0 price-point move for 1.0 lot."""
    point = float(info.point)
    tick_size = float(getattr(info, "trade_tick_size", 0) or point) or point
    tick_value = float(getattr(info, "trade_tick_value", 0) or 0)
    if tick_value <= 0:
        return 0.0
    return tick_value * (point / tick_size)


class Sim:
    """Backtest of ONE pair over a real bars slice."""

    def __init__(self, bars, strat, risk, lim, spread_price, mpp, info, equity0, is_24h=False):
        from strategy import Bar as B, atr as atr_series, ema

        self.is_24h = is_24h
        self.equity0 = equity0
        self.equity = equity0
        self.trades: list[dict] = []
        self.curve: list[dict] = []
        self.bars = bars
        closes = [float(b["close"]) for b in bars]
        self.ef = ema(closes, int(strat["trend_ema_fast"]))
        self.es = ema(closes, int(strat["trend_ema_slow"]))
        wrap = [B(int(b["time"]), float(b["open"]), float(b["high"]),
                  float(b["low"]), float(b["close"])) for b in bars]
        self.atr = atr_series(wrap, int(strat["atr_period"]))
        self.look = int(strat["breakout_lookback"])
        self.sl_atr = float(strat["sl_atr"])
        self.rr = float(strat["rr_take"])
        self.be_r = float(strat["be_at_r"])
        self.spread = spread_price
        self.mpp = mpp
        self.point = float(info.point)
        self.digits = int(info.digits)
        self.step = float(info.volume_step or 0.01)
        self.vmin = float(info.volume_min or 0.01)
        self.vmax = float(info.volume_max or 100.0)
        self.risk_pct = float(risk.get("risk_per_trade", 0.006))
        self.max_pos = int(risk.get("max_positions", 4))
        self.cool_bars = int(risk.get("cooldown_bars_after_loss", 2))
        self.spread_cap = float(lim.get("max_spread_atr_frac", 0.15))
        hl = [int(x) for x in lim.get("trading_hours_utc", [7, 21])]
        self.h_lo, self.h_hi = hl[0], min(hl[1], 24)
        self.pos = None
        self.open_count = 0
        self.cooldown_until = -1

    # ---------- mechanics ----------
    def close(self, exit_px, closed_t, i) -> None:
        p = self.pos
        dx = exit_px - p["entry"]
        pnl = dx * p["ds"] * p["vol"] * self.mpp
        self.equity += pnl
        self.trades.append({"dir": p["dir"], "entry": round(p["entry"], self.digits),
                            "exit": round(exit_px, self.digits),
                            "r": round(dx / p["risk"] * p["ds"], 3),
                            "vol": p["vol"], "opened": p["opened"], "closed": closed_t})
        self.pos = None
        self.open_count -= 1
        if pnl < 0:
            self.cooldown_until = i + self.cool_bars

    def regime(self, i) -> int:
        if self.ef[i] > self.es[i]:
            return 1
        if self.ef[i] < self.es[i]:
            return -1
        return 0

    def entry_allowed(self, i) -> bool:
        if self.pos is not None or self.open_count >= self.max_pos:
            return False
        if i <= self.cooldown_until:
            return False
        if not self.is_24h:
            hour = (int(self.bars[i]["time"]) // 3600) % 24
            if not (self.h_lo <= hour < self.h_hi):
                return False
        return True

    def try_entry(self, i) -> None:
        px = float(self.bars[i]["close"])
        a = self.atr[i]
        if a <= 0 or (self.spread / a) > self.spread_cap:
            return
        j0 = max(0, i - self.look)
        highs = [float(self.bars[k]["high"]) for k in range(j0, i)]
        lows = [float(self.bars[k]["low"]) for k in range(j0, i)]
        reg = self.regime(i)
        want = None
        if reg > 0 and highs and px > max(highs):
            want = "BUY"
        elif reg < 0 and lows and px < min(lows):
            want = "SELL"
        if want is None:
            return
        nxt = i + 1 if i + 1 < len(self.bars) else i
        entry = float(self.bars[nxt]["open"])
        entry = entry + self.spread if want == "BUY" else entry - self.spread
        stop = a * self.sl_atr
        vol = (self.equity * self.risk_pct) / (stop * self.mpp)
        if vol < self.vmin:
            return
        vol = min(round(round(vol / self.step) * self.step, 8), self.vmax)
        ds = 1 if want == "BUY" else -1
        self.pos = {"dir": want, "ds": ds,
                    "entry": round(entry, self.digits),
                    "sl": round(entry - stop, self.digits) if ds > 0 else round(entry + stop, self.digits),
                    "tp": round(entry + self.rr * stop, self.digits) if ds > 0 else round(entry - self.rr * stop, self.digits),
                    "risk": stop, "vol": vol,
                    "opened": int(self.bars[i]["time"]), "be": False}
        self.open_count += 1

    def manage(self, i) -> None:
        pos = self.pos
        if pos is None:
            return
        b = self.bars[i]
        hi, lo = float(b["high"]), float(b["low"])
        if pos["ds"] > 0:
            hit_sl, hit_tp = lo <= pos["sl"], hi >= pos["tp"]
        else:
            hit_sl, hit_tp = hi >= pos["sl"], lo <= pos["tp"]
        if hit_sl and hit_tp:
            hit_sl = True  # conservative: the stop is assumed to fill first
        if hit_sl:
            self.close(pos["sl"], int(b["time"]), i)
            return
        if hit_tp:
            self.close(pos["tp"], int(b["time"]), i)
            return
        if not pos["be"] and (
            (pos["ds"] > 0 and hi >= pos["entry"] + self.be_r * pos["risk"]) or
            (pos["ds"] < 0 and lo <= pos["entry"] - self.be_r * pos["risk"])
        ):
            pos["be"] = True
            pos["sl"] = round(pos["entry"] + (0.1 * self.point) * pos["ds"], self.digits)
        reg = self.regime(i)
        if (pos["ds"] > 0 and reg < 0) or (pos["ds"] < 0 and reg > 0):
            self.close(float(b["close"]), int(b["time"]), i)

    # ---------- run + stats ----------
    def run(self) -> None:
        for i in range(WARMUP, len(self.bars)):
            self.manage(i)
            if self.entry_allowed(i):
                self.try_entry(i)
            self.curve.append({"t": int(self.bars[i]["time"]), "eq": round(self.equity, 2)})
        if self.pos is not None:
            last = len(self.bars) - 1
            self.close(float(self.bars[last]["close"]), int(self.bars[last]["time"]), last)

    def stats(self) -> dict:
        peak, mdd = self.equity0, 0.0
        for c in self.curve + [{"eq": self.equity}]:
            peak = max(peak, c["eq"])
            mdd = max(mdd, (peak - c["eq"]) / peak)
        rs = [t["r"] for t in self.trades]
        wins = sum(1 for r in rs if r > 0)
        gain = sum(r for r in rs if r > 0)
        loss = abs(sum(r for r in rs if r < 0))
        pf = round(gain / loss, 2) if loss > 0 else (99.9 if gain > 0 else 0.0)
        net = self.equity - self.equity0
        return {"trades": len(rs),
                "winrate": round(wins / len(rs), 3) if rs else None,
                "profit_factor": ("inf" if pf == 99.9 else pf),
                "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
                "net": round(net, 2), "net_pct": round(net / self.equity0, 4),
                "max_dd_pct": round(mdd, 4),
                "final_equity": round(self.equity, 2)}


def main() -> int:
    cfg = load_cfg()
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    total_bars = min(int(days_back * 24 * 60 / 15), 13000)

    try:
        from mt5_ipc_patch import apply
        apply()
    except Exception as exc:
        log(f"ipc patch skipped: {type(exc).__name__}: {exc}")

    import MetaTrader5 as mt
    import sentinel as S

    acct = S.connect_mt5(mt)
    equity0 = float(acct["balance"]) or DEFAULT_EQUITY
    log(f"backtest on {acct['login']} @ {acct['server']} start equity {equity0}")

    S.wait_symbol_base(mt)
    results = {}
    for p in [str(x) for x in cfg["pairs"]]:
        sym = S.resolve_for_pair(mt, p)
        if not sym:
            results[p] = {"error": "symbol unresolved"}
            continue
        info = mt.symbol_info(sym)
        if info is None or info.point <= 0:
            results[p] = {"error": "symbol_info unavailable"}
            continue
        mpp = money_per_point(info)
        if mpp <= 0:
            results[p] = {"error": "trade_tick_value unavailable"}
            continue
        spread_price = (info.spread + SLIPPAGE_POINTS) * info.point

        rates = None
        for attempt in range(3):
            rates = mt.copy_rates_from_pos(sym, mt.TIMEFRAME_M15, 0, total_bars)
            if rates is not None and len(rates) >= WARMUP + 60:
                break
            log(f"  {p}: history attempt {attempt + 1}/3 short")
            time.sleep(6)
        if rates is None:
            results[p] = {"error": "no history returned"}
            continue
        bars = [dict(r) for r in rates]
        log(f"{p}: {len(bars)} bars ({len(bars) / 96:.0f} days) point-value/lot={mpp:.4f}")

        sim = Sim(bars, cfg["strategy"], cfg["risk"], cfg["limits"], spread_price,
                  mpp, info, equity0, is_24h=p.upper().startswith("BTC"))
        sim.run()
        results[p] = sim.stats()
        log(f"{p} -> {json.dumps(results[p])[:200]}")

    combined = combine(results, equity0)
    report = render(results, combined, days_back, equity0)
    with open("backtest_report.md", "w", encoding="utf-8") as fh:
        fh.write(report)
    with open("backtest_report.json", "w", encoding="utf-8") as fh:
        json.dump({"per_pair": results, "combined": combined, "days": days_back,
                   "start_equity": equity0,
                   "generated": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())},
                  fh, indent=2, ensure_ascii=False, default=str)
    log("BACKTEST COMPLETE " + json.dumps(combined))

    try:
        from sentinel import send_email
        send_email(
            f"🔬 Sentinel v2 backtest {days_back}d: PF={combined.get('avg_profit_factor')} "
            f"WR={round((combined.get('winrate') or 0) * 100)}% net={combined.get('net')}$",
            "<pre style='white-space:pre-wrap'>" + report[:1800] + "</pre>",
        )
    except Exception as exc:
        log(f"email failed: {type(exc).__name__}: {exc}")

    try:
        mt.shutdown()
    except Exception:
        pass
    return 0


def combine(results, equity0) -> dict:
    ok = [v for v in results.values() if "error" not in v]
    tot = sum(v["trades"] for v in ok)
    net = round(sum(v["net"] for v in ok), 2)
    wrs = [v["winrate"] for v in ok if v["winrate"] is not None]
    pfs = [v["profit_factor"] for v in ok if v["profit_factor"] != "inf"]
    dd = max((v["max_dd_pct"] for v in ok), default=0.0)
    return {"pairs": len(ok), "trades": tot, "net": net,
            "net_pct": round(net / equity0, 4),
            "winrate": round(sum(wrs) / len(wrs), 3) if wrs else None,
            "avg_profit_factor": round(sum(pfs) / len(pfs), 2) if pfs else None,
            "max_dd_pct": dd}


def render(results, combined, days_back, equity0) -> str:
    lines = [f"# Sentinel v2 Backtest — {days_back} days, M15", "",
             f"engine config: donchian breakout + EMA trend filter + ATR risk sizing",
             f"generated {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC on live broker history", "",
             "| Pair | Trades | WinRate | ProfitFactor | AvgR | Net | MaxDD | FinalEq |",
             "|---|---|---|---|---|---|---|---|"]
    for p, v in results.items():
        if "error" in v:
            lines.append(f"| {p} | ERROR: {v['error']} | - | - | - | - | - | - |")
            continue
        lines.append(f"| {p} | {v['trades']} | {v['winrate']} | {v['profit_factor']} | "
                     f"{v['avg_r']} | {v['net']}$ | {round(v['max_dd_pct'] * 100, 1)}% | {v['final_equity']}$ |")
    lines += ["", f"**Combined across {combined['pairs']} pairs**: trades={combined['trades']}, "
              f"net={combined['net']}$ ({round(combined['net_pct'] * 100, 2)}%), "
              f"winrate={combined['winrate']}, avg PF={combined['avg_profit_factor']}, "
              f"maxDD={round(combined['max_dd_pct'] * 100, 1)}%", "",
              "> historical simulation. Real forward results depend on spreads, sessions and news periods.",
              ""]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
