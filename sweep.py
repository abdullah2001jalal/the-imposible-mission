"""Offline parameter sweep for Sentinel v2 on committed history dumps.

Needs history/M15_<PAIR>.csv.gz (+ _meta.json) produced by
`python backtest.py <days> dump` on a broker-connected machine
(then committed to the repo).

Scoring is robust-by-design:
- everything is r-multiple based (position sizing cancels out),
- each config is scored on BOTH halves of the sample (first ~60d, last ~60d);
  a config counts only if both halves are >= the floor,
- rank = combined PF of the halves' floors.

Usage:
    python sweep.py                # stage 1: global config grid
    python sweep.py --refine       # stage 2: per-pair refinement
"""
from __future__ import annotations

import itertools
import os
import sys
from multiprocessing import Pool

from backtest import Sim, load_history

PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "BTCUSD"]
HALF_FLOOR = 0.90          # each half PF must beat this to qualify
MIN_TRADES = 25            # sanity sample size (per pair, per config)

RISK = {"risk_per_trade": 0.006, "max_positions": 4, "cooldown_bars_after_loss": 2}
LIM = {"max_spread_atr_frac": 0.15, "trading_hours_utc": [7, 21]}

BASE = {"trend_ema_fast": 50, "trend_ema_slow": 200, "atr_period": 14,
        "breakout_lookback": 24, "sl_atr": 1.5, "rr_take": 2.0,
        "be_at_r": 1.0, "exit_flip_trend": True}

INDEX = {}


def init_index():
    for p in PAIRS:
        INDEX[p] = load_history(p)


def split_bars(bars):
    """Decision bars start at index 230; split the decision range in half."""
    s = 230
    mid = s + (len(bars) - s) // 2
    first = bars[: mid]
    second = bars[max(0, mid - 230):]   # keep warmup context for half 2
    return first, second


def run_pair(cfg):
    pair, strat = cfg
    bars, info = INDEX[pair]
    ppv = float(getattr(info, "trade_tick_value", 0) or 0) / float(
        getattr(info, "trade_tick_size", 0) or info.point)
    spread = (info.spread if getattr(info, "spread", 0) else 12) * info.point + 2 * info.point

    def _stats(strip):
        if len(strip) <= 240:
            return None
        sim = Sim(strip, strat, RISK, LIM, spread, None, info, 10000.0,
                  is_24h=pair.upper().startswith("BTC"))
        sim.run()
        return sim.stats()

    first, second = split_bars(bars)
    st = _stats(bars)
    h1 = _stats(first)
    h2 = _stats(second)
    return pair, strat, st, h1, h2


def grid_stage1():
    combos = []
    for look, sl, rr, be, flip in itertools.product(
            (24, 48, 72),          # breakout lookback
            (1.0, 1.5, 2.0, 3.0),  # sl_atr
            (1.5, 2.0, 3.0),       # rr_take
            (1.0, None),           # be_at_r (None = no BE)
            (True, False)):
        # be disabled: strategy moves SL at r_mult >= be_at_r -> huge value = never
        s = dict(BASE, breakout_lookback=look, sl_atr=sl, rr_take=rr,
                 be_at_r=(99.0 if be is None else be), exit_flip_trend=flip)
        combos.append(s)
    return combos


def grid_refine(best_per_pair):
    """Small neighborhood around each pair's global winner."""
    out = []
    for look, sl, rr in best_per_pair:
        for dLook, dSl in ((0, 0), (12, 0.5), (-12, -0.5), (24, 1.0), (-24, 1.0), (0, 0.5), (0, -0.5)):
            look2 = max(8, look + dLook)
            sl2 = max(0.5, round(sl + dSl, 1))
            out.append(dict(BASE, breakout_lookback=look2, sl_atr=sl2, rr_take=rr,
                            be_at_r=1.0, exit_flip_trend=True))
    return out


def to_key(strat):
    return (strat["breakout_lookback"], strat["sl_atr"], strat["rr_take"],
            strat["be_at_r"], strat["exit_flip_trend"])


def score_one(pair, strat, st, h1, h2):
    if not st or not h1 or not h2:
        return None
    if (st["trades"] + h1["trades"] + h2["trades"]) < MIN_TRADES:
        return None
    pf1 = h1["profit_factor"]
    pf2 = h2["profit_factor"]
    if pf1 == "inf":
        pf1 = 3.5
    if pf2 == "inf":
        pf2 = 3.5
    if pf1 < HALF_FLOOR or pf2 < HALF_FLOOR:
        return None
    pfc = max(min(pf1, pf2), 0.01)
    return {"pair": pair, "cfg": to_key(strat), "pf": st["profit_factor"],
            "pf_h1": pf1, "pf_h2": pf2, "score": pfc,
            "trades": st["trades"], "avg_r": st["avg_r"]}


def evaluate(configs, workers):
    tasks = [(p, s) for p in PAIRS for s in configs]
    rows = []
    with Pool(workers, initializer=init_index) as pool:
        for pair, strat, st, h1, h2 in pool.imap_unordered(run_pair, tasks, chunksize=4):
            r = score_one(pair, strat, st, h1, h2)
            if r:
                rows.append(r)
    return rows


def report(rows, title, top=25):
    print(f"\n== {title}: {len(rows)} qualifying configs ==")
    rows.sort(key=lambda r: (r["score"], r["pf"]), reverse=True)
    print(f"{'pair':7} {'look':>4} {'sl':>4} {'rr':>4} {'be':>5} {'flip':>4} | "
          f"{'PF':>5} {'PF_h1':>5} {'PF_h2':>5} {'trades':>6} {'avgR':>6}")
    for r in rows[:top]:
        cfg = r["cfg"]
        print(f"{r['pair']:7} {cfg[0]:>4} {cfg[1]:>4} {cfg[2]:>4} {cfg[3]:>5} {str(cfg[4])[:1]:>4} | "
              f"{r['pf']:>5} {r['pf_h1']:>5} {r['pf_h2']:>5} {r['trades']:>6} {r['avg_r']:>6}")
    return rows[:top]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--refine", action="store_true")
    ap.add_argument("--workers", type=int, default=min(os.cpu_count() or 2, 8))
    a = ap.parse_args()

    for p in PAIRS:
        assert os.path.isfile(f"history/M15_{p}.csv.gz"), f"missing dump for {p}"

    if a.refine:
        # hardcode a few global winners discovered in stage 1 here
        seed = [(24, 2.0, 2.0), (48, 2.0, 2.0), (72, 2.0, 3.0)]
        rows = evaluate(grid_refine(seed), a.workers)
        report(rows, "PER-PAIR refinement")
        return 0

    rows = evaluate(grid_stage1(), a.workers)
    best = report(rows, "GLOBAL stage-1")
    # per-pair winners among stage-1 rows
    by_pair = {}
    for r in rows:
        if r["pair"] not in by_pair or r["score"] > by_pair[r["pair"]]["score"]:
            by_pair[r["pair"]] = r
    print("\n== per-pair best from the global grid ==")
    for p in PAIRS:
        r = by_pair.get(p)
        print(p, "->", (r["cfg"], round(r["pf"], 2), r["trades"]) if r else "NONE qualifies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
