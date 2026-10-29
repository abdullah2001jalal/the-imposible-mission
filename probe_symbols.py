"""Low-overhead CI probe: prints exact broker specifications for the pairs.

Run via .github/workflows/probe.yml (manual dispatch). Output goes to the job
log; used to calibrate risk sizing (money per point per lot, volume clamps).
"""
from __future__ import annotations

import sys
import time

FIELDS = ("point", "trade_tick_size", "trade_tick_value", "volume_min",
          "volume_max", "volume_step", "volume_limit", "trade_stops_level",
          "spread", "digits")


def main() -> int:
    try:
        from mt5_ipc_patch import apply
        apply()
    except Exception as exc:
        print(f"ipc patch skipped: {type(exc).__name__}: {exc}")
    import MetaTrader5 as mt
    import sentinel as S

    acct = S.connect_mt5(mt)
    print(f"account {acct['login']} @ {acct['server']} balance={acct['balance']}")
    S.wait_symbol_base(mt, 90)
    for p in ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "BTCUSD"]:
        sym = S.resolve_for_pair(mt, p)
        print(f"--- {p} -> {sym}")
        if not sym:
            continue
        info = mt.symbol_info(sym)
        if info is None:
            print(f"{p}: symbol_info None")
            continue
        print(f"{p} description={getattr(info, 'description', '')}")
        for f in FIELDS:
            print(f"{p} {f}={getattr(info, f, 'N/A')}")
        print(f"{p} money_per_point(computed)={S.money_per_point_required(info):.6f}"
              if hasattr(S, "money_per_point_required") else "")
        print(f"{p} mpp_formula=tick_value*(point/tick_size)="
              f"{(getattr(info, 'trade_tick_value', 0) * (info.point / getattr(info, 'trade_tick_size', info.point))):.6f}")
        rates = mt.copy_rates_from_pos(sym, mt.TIMEFRAME_M15, 0, 30)
        if rates is not None:
            last3 = [float(r["close"]) for r in rates[-3:]]
            print(f"{p} last 3 closes={last3}")
        tick = mt.symbol_info_tick(sym)
        if tick is not None:
            print(f"{p} tick bid={tick.bid} ask={tick.ask}")
        print(flush=True)
    try:
        mt.shutdown()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
