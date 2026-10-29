"""SENTINEL v2 — multi-pair persistent trading orchestrator.

Complete re-engineering of the v1 bot. Key differences:
- NO LLM in the trading path; deterministic strategy core (strategy.py).
- TRUE position management: every run reconciles open positions
  (breakeven move, regime-flip exit) instead of fire-and-forget.
- Multi-pair from config.yml (the repo IS the control panel).
- Full persistence: state.json snapshot + journal/YYYY-MM-DD.jsonl decision
  log committed after every run; restarts resume where everything stopped.
- Modes: paper (no orders) / demo / live — plus the global halt switch
  (halt=true also stops the self-perpetuating chain within one run).
- Quiet alerts: executed orders, daily summary, critical errors only.

Proven v1 tech kept intact: mt5_ipc_patch IAT hook, connect chain
(config-launch auto-login + AutoTrading ON), suffix-safe symbol resolution,
race-proof state push (in workflow), UTF-16 terminal journal reader.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request

import calendar
import yaml

STATE_FILE = "state.json"
CONFIG_FILE = "config.yml"
STATUS_FILE = "engine_status.json"
ALERT_FLAG = "ALERT_SENT.flag"
JOURNAL_DIR = "journal"
STATE_SCHEMA = "sentinel-v2"
MAGIC = 2911001
BAR_COUNT = 260          # M15 bars: EMA200 + Donchian + buffer
BAR_SECONDS = 15 * 60


def log(*a) -> None:
    print(f"[{time.strftime('%H:%M:%S')} UTC] " + " ".join(str(x) for x in a), flush=True)


def utc_parts() -> tuple[str, str, int]:
    now = time.gmtime()
    return time.strftime("%Y-%m-%d", now), time.strftime("%H:%M:%S", now), now.tm_wday


def run_link() -> str:
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not server:
        return ""
    return f'<a href="{server}/{repo}/actions/runs/{run_id}">run {run_id}</a>'


# --------------------------------------------------------------------------- #
# alerts (Resend; UA must be resend-python/2.0.0 or Cloudflare blocks it)
# --------------------------------------------------------------------------- #
def send_email(subject: str, html: str) -> bool:
    key = os.environ.get("RESEND_API_KEY") or ""
    to = os.environ.get("RECEIVER_EMAIL") or ""
    if not key or not to:
        log(f"EMAIL SKIP (no credentials): {subject}")
        return False
    payload = json.dumps({
        "from": "onboarding@resend.dev",
        "to": [to],
        "subject": subject,
        "html": html + (f"<p style='color:#999'>UTC {utc_parts()[1]} — {run_link()}</p>" if run_link() else ""),
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "resend-python/2.0.0",
        },
        method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status in (200, 202)
        except urllib.error.HTTPError as e:
            log(f"email http {e.code}: {e.read().decode()[:200]} ({attempt + 1}/3)")
            time.sleep(5)
        except Exception as e:
            log(f"email error {type(e).__name__}: {e} ({attempt + 1}/3)")
            time.sleep(5)
    return False


def email_actions(items: list[dict], mode: str) -> None:
    rows = "".join(
        f"<tr><td style='padding:5px 12px'><b>{k}</b></td><td style='padding:5px 12px'>{v}</td></tr>"
        for item in items for k, v in item.items()
    )
    send_email(
        f"💵 Sentinel v2 [{mode}]: action executed",
        f"<h3>Executed</h3><table border='1' style='border-collapse:collapse'>{rows}</table>",
    )


def email_critical(stage: str, exc: Exception) -> None:
    tb = traceback.format_exc(limit=6)
    send_email(
        f"🚨 Sentinel v2 CRITICAL: {type(exc).__name__} @ {stage}",
        f"<p><b>{type(exc).__name__}</b>: {str(exc)[:800]}</p>"
        f"<pre style='white-space:pre-wrap;background:#f4f4f4'>{tb[:2500]}</pre>",
    )
    with open(ALERT_FLAG, "w") as fh:  # tell the workflow a bot-level alert went out
        fh.write("1")


# --------------------------------------------------------------------------- #
# config / state / journal
# --------------------------------------------------------------------------- #
def load_config() -> tuple[dict, bool]:
    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg.get("pairs", []), list) or not cfg.get("pairs"):
        raise RuntimeError("config.yml: pairs must be a non-empty list")
    return cfg, bool(cfg.get("halt", True))


def fresh_state() -> dict:
    return {
        "schema": STATE_SCHEMA,
        "updated": "",
        "day": {"date": "", "start_equity": None},
        "summary_sent_day": "",
        "last_bar": {},
        "positions": {},
        "closed_log": [],
        "total_cycles": 0,
    }


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            st = json.load(fh)
        if st.get("schema") == STATE_SCHEMA:
            return st
        log(f"state schema={st.get('schema')!r} != {STATE_SCHEMA} -> fresh v2 state")
        return fresh_state()
    except FileNotFoundError:
        return fresh_state()
    except Exception as exc:
        log(f"state unreadable ({type(exc).__name__}: {exc}) -> fresh")
        return fresh_state()


def save_state(state: dict) -> None:
    state["updated"] = f"{utc_parts()[0]} {utc_parts()[1]}"
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str, ensure_ascii=False)
        fh.write("\n")


def write_status(state: dict, cfg: dict, halt: bool, summary: dict) -> None:
    """Small control-plane file the workflow's re-arm step reads."""
    with open(STATUS_FILE, "w", encoding="utf-8") as fh:
        json.dump({
            "halt": bool(halt),
            "mode": str(cfg.get("mode", "paper")),
            "total_cycles": int(state.get("total_cycles", 0)),
            "open_positions": int(summary.get("open_positions", 0)),
            "day_pnl_pct": summary.get("day_pnl_pct"),
        }, fh, indent=2)


def journal(event: dict) -> None:
    day = utc_parts()[0]
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    path = os.path.join(JOURNAL_DIR, f"{day}.jsonl")
    line = json.dumps({"t": f"{day} {utc_parts()[1]}", **event}, ensure_ascii=False, default=str)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    log("J:", line[:220])


# --------------------------------------------------------------------------- #
# MT5 layer (proven v1 tech, ported)
# --------------------------------------------------------------------------- #
def kill_terminals() -> None:
    import subprocess
    subprocess.run(["taskkill", "/f", "/im", "terminal64.exe"], capture_output=True, shell=False)


def find_terminal_exe():
    env_path = os.environ.get("MT5_PATH", "").strip()
    if env_path and os.path.isfile(env_path):
        return env_path
    for base in ("C:\\Program Files", "C:\\Program Files (x86)"):
        for d in glob.glob(os.path.join(base, "*")):
            cand = os.path.join(d, "terminal64.exe")
            if os.path.isfile(cand):
                return cand
    return None


def write_login_config(path: str | None = None) -> str:
    ini = (
        "[Common]\r\n"
        f"Login={os.environ.get('EXNESS_LOGIN')}\r\n"
        f"Password={os.environ.get('EXNESS_PASSWORD')}\r\n"
        f"Server={os.environ.get('EXNESS_SERVER')}\r\n"
        "ProxyEnable=0\r\nCertInstall=0\r\nNewsEnable=0\r\nKeepPrivate=1\r\n\r\n"
        "[Experts]\r\nAllowLiveTrading=1\r\nAllowDllImport=0\r\nEnabled=1\r\nAccount=0\r\nProfile=0\r\n"
    )
    if path is None:
        path = os.path.join(os.environ.get("RUNNER_TEMP") or os.getcwd(), "mt5login.ini")
    with open(path, "w", encoding="ascii", errors="replace", newline="") as fh:
        fh.write(ini)
    return path


def write_data_folder_config() -> str | None:
    base = os.path.join(os.environ.get("APPDATA", ""), "MetaQuotes", "Terminal")
    for d in glob.glob(os.path.join(base, "*")):
        if os.path.isdir(os.path.join(d, "config")):
            try:
                write_login_config(os.path.join(d, "config", "common.ini"))
                log(f"data-folder common.ini written: {d}")
                return d
            except Exception as exc:
                log(f"data-folder common.ini skip ({exc})")
    return None


def launch_terminal(config_path: str | None = None, portable: bool = False) -> None:
    import subprocess
    exe = find_terminal_exe()
    if exe is None:
        raise RuntimeError("terminal64.exe not found")
    args = [exe] + ([f"/config:{config_path}"] if config_path else []) + (["/portable"] if portable else [])
    log("launch:", " ".join(args))
    subprocess.Popen(args, cwd=os.path.dirname(exe))


def wait_ready(mt5_module, seconds: int = 90):
    deadline, last = time.time() + seconds, None
    while time.time() < deadline:
        term, acct = mt5_module.terminal_info(), mt5_module.account_info()
        if term is not None and acct is not None and getattr(term, "connected", False):
            log(f"MT5 CONNECTED: login={acct.login} server={acct.server} balance={acct.balance} equity={acct.equity}")
            return acct
        snap = f"connected={getattr(term, 'connected', None)} acct={'None' if acct is None else acct.login}"
        if snap != last:
            log("  wait_ready:", snap)
            last = snap
        time.sleep(3)
    return None


def connect_mt5(mt5_module) -> dict:
    login_env = os.environ.get("EXNESS_LOGIN")
    pwd, srv = os.environ.get("EXNESS_PASSWORD"), os.environ.get("EXNESS_SERVER")
    if not (login_env and pwd and srv):
        raise RuntimeError("EXNESS_LOGIN/PASSWORD/SERVER missing")
    login_int = int(login_env)
    creds = {"login": login_int, "password": pwd, "server": srv}
    strategies = [
        ("config-launch (auto-login + AutoTrading ON) + attach", "config_spawn"),
        ("module spawn with creds", "spawn"),
        ("config launch + attach", "config"),
    ]
    kill_terminals()
    time.sleep(3)
    err = None
    for name, mode in strategies:
        log("MT5 strategy:", name)
        try:
            if mode == "config_spawn":
                exe = find_terminal_exe()
                if exe is None:
                    raise RuntimeError("terminal64.exe not found")
                write_data_folder_config()
                launch_terminal(write_login_config())
                time.sleep(15)
                ok = mt5_module.initialize(path=exe, timeout=90000, **creds)
            elif mode == "spawn":
                exe = find_terminal_exe()
                if exe is None:
                    raise RuntimeError("terminal64.exe not found")
                ok = mt5_module.initialize(path=exe, timeout=90000, **creds)
            else:
                write_data_folder_config()
                launch_terminal(write_login_config())
                time.sleep(30)
                ok = mt5_module.initialize(timeout=90000)
            if ok:
                acct = wait_ready(mt5_module, 90)
                if acct is None:
                    log("  explicit mt5.login()...")
                    if mt5_module.login(login_int, password=pwd, server=srv):
                        acct = wait_ready(mt5_module, 60)
                if acct is not None:
                    return {"login": acct.login, "server": acct.server,
                            "balance": acct.balance, "equity": acct.equity}
                err = f"init ok but no account ({mt5_module.last_error()})"
            else:
                err = str(mt5_module.last_error())
            log("  ->", err)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            log("  -> strategy error:", err)
        try:
            mt5_module.shutdown()
        except Exception:
            pass
        kill_terminals()
        time.sleep(10)
    raise RuntimeError(f"MT5 connect failed after all strategies: {err}")


def wait_symbol_base(mt5_module, seconds: int = 120) -> None:
    deadline, total = time.time() + seconds, (mt5_module.symbols_total() or 0)
    while total <= 0 and time.time() < deadline:
        time.sleep(5)
        total = mt5_module.symbols_total() or 0
    log(f"symbols_total={total}")
    if total <= 0:
        log("  !! symbols base still empty — broker sync may have failed")


def resolve_for_pair(mt5_module, pair: str) -> str | None:
    if mt5_module.symbol_select(pair, True):
        return pair
    names = mt5_module.symbols_get() or []
    base = pair.upper()
    cands = sorted({s.name for s in names if s.name.upper().startswith(base)}, key=len)
    for cand in cands[:3]:
        if mt5_module.symbol_select(cand, True):
            log(f"resolved {pair} -> {cand}")
            return cand
    return None


def rows_to_dicts(rates) -> list[dict]:
    """Convert MetaTrader5's numpy structured records to plain dicts (native types)."""
    names = rates.dtype.names
    out = []
    for rec in rates:
        d = {}
        for n in names:
            v = rec[n]
            d[n] = v.item() if hasattr(v, "item") else v
        out.append(d)
    return out


def fetch_bars(mt5_module, timeframe, symbol: str) -> list[dict] | None:
    for attempt in range(1, 4):
        rates = mt5_module.copy_rates_from_pos(symbol, timeframe, 0, BAR_COUNT)
        if rates is not None and len(rates) >= 230:
            return rows_to_dicts(rates)
        log(f"rates for {symbol} attempt {attempt}/3 short ({mt5_module.last_error()})")
        time.sleep(8)
    return None


def risk_volume(symbol_info, equity: float, stop_dist: float, risk_pct: float) -> float:
    """Volume so a full stop-out loses ~risk_pct of equity."""
    if stop_dist <= 0 or equity <= 0:
        return 0.0
    try:
        point = float(symbol_info.point)
        tick_size = float(getattr(symbol_info, "trade_tick_size", 0) or point) or point
        tick_value = float(getattr(symbol_info, "trade_tick_value", 0) or 0)
        if tick_value <= 0 or point <= 0:
            return 0.0
        money_per_point_per_lot = tick_value * (point / tick_size)
        vol = (equity * risk_pct) / (stop_dist * money_per_point_per_lot)
    except Exception as exc:
        log(f"risk_volume error {type(exc).__name__}: {exc}")
        return 0.0
    step = float(symbol_info.volume_step or 0.01)
    vmin = float(symbol_info.volume_min or 0.01)
    vmax = float(symbol_info.volume_max or 100.0)
    if vol < vmin:
        return 0.0
    return min(max(round(round(vol / step) * step, 8), vmin), vmax)


def order_close(mt5_module, pos, comment: str = "SV2") -> dict:
    tick = mt5_module.symbol_info_tick(pos.symbol)
    price = tick.bid if pos.type == mt5_module.ORDER_TYPE_BUY else tick.ask
    req = {
        "action": mt5_module.TRADE_ACTION_DEAL,
        "position": pos.ticket,
        "symbol": pos.symbol,
        "volume": float(pos.volume),
        "type": mt5_module.ORDER_TYPE_SELL if pos.type == mt5_module.ORDER_TYPE_BUY else mt5_module.ORDER_TYPE_BUY,
        "price": float(price),
        "deviation": 40,
        "magic": MAGIC,
        "comment": comment,
        "type_time": mt5_module.ORDER_TIME_GTC,
        "type_filling": mt5_module.ORDER_FILLING_IOC,
    }
    res = mt5_module.order_send(req)
    return {"ticket": int(pos.ticket), "retcode": int(res.retcode) if res else -1,
            "comment": str(res.comment) if res else "None"}


def order_modify_sl(mt5_module, pos, new_sl: float) -> int:
    req = {
        "action": mt5_module.TRADE_ACTION_SLTP,
        "position": int(pos.ticket),
        "symbol": pos.symbol,
        "sl": float(new_sl),
        "tp": float(pos.tp or 0),
    }
    res = mt5_module.order_send(req)
    return int(res.retcode) if res else -1


# --------------------------------------------------------------------------- #
# main pipeline
# --------------------------------------------------------------------------- #
def run_cycles(cfg: dict, mt5_module, state: dict, halt_bit: bool) -> dict:
    from strategy import Bar, PositionCtx, entry_signal, manage_position

    tf_name = str(cfg.get("timeframe", "M15")).upper()
    tf = getattr(mt5_module, f"TIMEFRAME_{tf_name}", None)
    if tf is None:
        raise RuntimeError(f"bad TIMEFRAME {tf_name}")
    mode = str(cfg.get("mode", "paper")).lower()
    if mode not in ("paper", "demo", "live"):
        mode = "paper"

    strat_cfg = {**cfg.get("strategy", {})}
    lim = {**cfg.get("limits", {})}
    risk_cfg = {**cfg.get("risk", {})}

    today, hhmmss, wday = utc_parts()
    hour, minute = int(hhmmss.split(":")[0]), int(hhmmss.split(":")[1])

    # ---- account snapshot BEFORE connect (cheap first read) ----
    boot = None
    try:
        boot = mt5_module.account_info()
    except Exception:
        boot = None

    mt5_module.shutdown()
    acct = connect_mt5(mt5_module)
    equity = float(acct["equity"])
    balance = float(acct["balance"])

    # ---- day bookkeeping / daily stop ----
    dayinfo = state.get("day") or {}
    if dayinfo.get("date") != today:
        state["day"] = {"date": today, "start_equity": equity}
        log(f"new UTC day {today} -> day start equity {equity}")
        dayinfo = state["day"]
    start_eq = float(dayinfo.get("start_equity") or equity) or equity
    day_pnl = (equity - start_eq) / start_eq if start_eq else 0.0
    day_stopped = day_pnl <= float(risk_cfg.get("day_stop_pnl", -0.015))
    log(f"equity={equity:.2f} balance={balance:.2f} day_pnl={day_pnl * 100:.2f}% "
        f"mode={mode} day_stop={'HIT' if day_stopped else 'ok'}")

    wait_symbol_base(mt5_module)
    pairs = [str(p) for p in cfg["pairs"]]
    symbol_of: dict[str, str] = {}
    for p in pairs:
        sym = resolve_for_pair(mt5_module, p)
        if sym is None:
            log(f"!! pair {p}: no broker symbol — skipped this cycle")
        symbol_of[p] = sym

    live = {int(pos.ticket): pos for pos in (mt5_module.positions_get() or [])}
    book: dict = state.setdefault("positions", {})
    state["total_cycles"] = int(state.get("total_cycles", 0)) + 1
    journal({"ev": "cycle", "mode": mode, "open": len(live), "equity": round(equity, 2),
             "day_pnl": round(day_pnl, 4), "halt": halt_bit,
             "resolved": symbol_of})

    # ---- reconcile positions vanished from the broker ----
    for ticket in list(book.keys()):
        if int(ticket) not in live:
            info = book.pop(str(ticket))
            closes = state.setdefault("closed_log", [])
            closes.append({"ticket": ticket, "pair": info.get("pair"), "entry": info.get("entry"),
                           "opened": info.get("opened"), "closed": f"{today} {hhmmss}"})
            state["closed_log"] = closes[-25:]
            journal({"ev": "closed", "ticket": ticket, "info": info})

    # ---- adopt orphan positions on managed pairs (e.g. left by v1) ----
    for ticket, pos in live.items():
        if str(ticket) in book:
            continue
        owner = next((p for p, s in symbol_of.items() if s == pos.symbol), None)
        if owner is None:
            journal({"ev": "foreign_position", "ticket": ticket, "symbol": pos.symbol})
            continue
        entry = float(pos.price_open)
        sl = float(pos.sl or 0)
        stop_dist = abs(entry - sl) if sl > 0 else 0.0
        book[str(ticket)] = {"pair": owner, "symbol": pos.symbol, "entry": entry,
                             "stop_points": stop_dist, "be_moved": False,
                             "opened": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(getattr(pos, "time", time.time()))),
                             "adopted": True}
        journal({"ev": "adopted", "ticket": ticket, "pair": owner, "entry": entry,
                 "sl": sl, "note": "orphan adopted from previous engine"})

    # ---- session gates ----
    l_lo, l_hi = [int(x) for x in lim.get("trading_hours_utc", [7, 21])]
    in_hours = l_lo <= hour < min(l_hi, 24)
    friday_stop = (wday == 4 and hour >= int(lim.get("skip_friday_after_utc", 20)))

    executed: list[dict] = []

    # ---- manage open book positions ----
    for ticket_s, ctx in list(book.items()):
        pos = live.get(int(ticket_s))
        if pos is None:
            continue
        pair = symbol_of.get(ctx.get("pair", ""), None)
        if pair is None or pos.symbol != pair:
            log(f"skip manage ticket {ticket_s}: symbol mismatch")
            continue
        bars = fetch_bars(mt5_module, tf, pair)
        if not bars:
            continue
        bs = [Bar(time=int(b["time"]), open=float(b["open"]), high=float(b["high"]),
                  low=float(b["low"]), close=float(b["close"])) for b in bars]
        direction = "BUY" if int(pos.type) == mt5_module.ORDER_TYPE_BUY else "SELL"
        info = mt5_module.symbol_info(pair)
        if ctx.get("stop_points", 0) <= 0 and info is not None and info.point:
            atr_last = atr_fallback(bs)
            ctx["stop_points"] = atr_last * float(cfg.get("strategy", {}).get("sl_atr", 1.5))
        pc = PositionCtx(ticket=int(ticket_s), pair=pair, direction=direction,
                         entry=float(pos.price_open), stop_points=float(ctx.get("stop_points", 0)),
                         volume=float(pos.volume), be_moved=bool(ctx.get("be_moved")))
        act = manage_position(pc, bs, strat_cfg)
        journal({"ev": "manage", "ticket": ticket_s, "pair": pair, "plan": act.action,
                 "r": act.extras.get("r_multiple"), "why": act.reason})
        if act.action == "CLOSE":
            if mode == "paper":
                continue
            res = order_close(mt5_module, pos, comment="SV2 exit")
            if res["retcode"] in (10009, 10008, 10010):
                executed.append({"Action": "CLOSE", "Pair": pair, "Ticket": ticket_s, "Why": act.reason})
                info_b = book.pop(ticket_s)
                closes = state.setdefault("closed_log", [])
                closes.append({"ticket": ticket_s, "pair": pair, "entry": info_b.get("entry"),
                               "opened": info_b.get("opened"), "closed": f"{today} {hhmmss}"})
                state["closed_log"] = closes[-25:]
                journal({"ev": "executed", **res, "act": "close"})
            else:
                journal({"ev": "close_failed", **res})
        elif act.action == "BE":
            if mode == "paper":
                journal({"ev": "plan_be", "ticket": ticket_s})
                continue
            new_sl = be_price(info, float(pos.price_open), direction)
            if new_sl is None:
                continue
            cur_sl = float(pos.sl or 0)
            better = (direction == "BUY" and new_sl > cur_sl) or (direction == "SELL" and (cur_sl == 0 or new_sl < cur_sl))
            if not better:
                continue
            rc = order_modify_sl(mt5_module, pos, new_sl)
            if rc in (10009, 10008, 10010):
                ctx["be_moved"] = True
                executed.append({"Action": "BE-SL", "Pair": pair, "Ticket": ticket_s, "NewSL": new_sl})
                journal({"ev": "executed", "act": "be", "ticket": ticket_s, "new_sl": new_sl, "retcode": rc})
            else:
                journal({"ev": "be_failed", "ticket": ticket_s, "retcode": rc})

    # ---- entries ----
    # refresh the live book: manage-phase closes free slots for new entries
    try:
        live = {int(pos.ticket): pos for pos in (mt5_module.positions_get() or [])}
    except Exception:
        pass
    open_count = len([int(t) for t in book if int(t) in live])
    max_pos = int(risk_cfg.get("max_positions", 4))
    per_pair = int(risk_cfg.get("max_per_pair", 1))
    cool_bars = int(risk_cfg.get("cooldown_bars_after_loss", 2))
    spread_cap = float(lim.get("max_spread_atr_frac", 0.15))

    for p in pairs:
        pair = symbol_of[p]
        if pair is None:
            continue
        symbol_pos = [pos for pos in live.values() if pos.symbol == pair and str(pos.ticket) in book]
        symbol_pos_total = [pos for pos in live.values() if pos.symbol == pair]
        if len(symbol_pos_total) >= per_pair or open_count >= max_pos or day_stopped:
            continue
        last_state = state["last_bar"].get(p)
        bars = fetch_bars(mt5_module, tf, pair)
        if not bars:
            continue
        bs = [Bar(time=int(b["time"]), open=float(b["open"]), high=float(b["high"]),
                  low=float(b["low"]), close=float(b["close"])) for b in bars]
        bar_t = int(bs[-1].time)
        if last_state == bar_t:
            continue  # same M15 bar already processed
        state["last_bar"][p] = bar_t

        # cooldown after a recent closed loss on this pair
        cl = (state.get("closed_log") or [])
        recent = next((c for c in reversed(cl) if c.get("pair") == pair), None)
        if recent:
            try:
                closed_t = calendar.timegm(time.strptime(recent["closed"], "%Y-%m-%d %H:%M:%S"))
                if 0 < time.time() - closed_t < cool_bars * BAR_SECONDS:
                    journal({"ev": "cooldown", "pair": p, "recent_close": recent["closed"]})
                    continue
            except Exception:
                pass

        if not (in_hours or p.upper().startswith("BTC")) or friday_stop:
            journal({"ev": "session_skip", "pair": p})
            continue

        action, atr_val = entry_signal(bs, strat_cfg)
        if action == "NONE":
            journal({"ev": "no_signal", "pair": p})
            continue
        info = mt5_module.symbol_info(pair)
        if info is None or info.point <= 0:
            continue
        spread_price = info.spread * info.point
        if atr_val > 0 and spread_price / atr_val > spread_cap:
            journal({"ev": "skip_spread", "pair": p, "spread_spread": round(spread_price, 6), "atr": round(atr_val, 6)})
            continue
        min_dist = float(getattr(info, "trade_stops_level", 0) or 0) + 2 * info.point
        stop_dist = max(atr_val, min_dist)
        equity_now = equity
        try:
            acct_now = mt5_module.account_info()
            if acct_now is not None:
                equity_now = float(acct_now.equity)
        except Exception:
            pass
        vol = risk_volume(info, equity_now, stop_dist, float(strat_cfg.get("risk_per_trade", risk_cfg.get("risk_per_trade", 0.006))))
        if vol <= 0:
            journal({"ev": "skip_size", "pair": p, "stop_dist": round(stop_dist, 6)})
            continue
        journal({"ev": "signal", "pair": p, "act": action, "stop_dist": round(stop_dist, 6),
                 "vol": vol, "mode": mode, "atr": round(atr_val, 6)})

        if mode == "paper":
            executed.append({"Action": f"{action}(paper)", "Pair": p, "Vol": vol})
            continue

        tick = mt5_module.symbol_info_tick(pair)
        if tick is None or tick.ask <= 0:
            journal({"ev": "no_tick", "pair": p})
            continue
        digits = info.digits
        if action == "BUY":
            price, sl, tp = tick.ask, round(tick.ask - stop_dist, digits), round(tick.ask + float(strat_cfg.get("rr_take", 2.0)) * stop_dist, digits)
        else:
            price, sl, tp = tick.bid, round(tick.bid + stop_dist, digits), round(tick.bid - float(strat_cfg.get("rr_take", 2.0)) * stop_dist, digits)

        for fill in (mt5_module.ORDER_FILLING_FOK, mt5_module.ORDER_FILLING_IOC, mt5_module.ORDER_FILLING_RETURN):
            req = {
                "action": mt5_module.TRADE_ACTION_DEAL,
                "symbol": pair,
                "volume": float(vol),
                "type": mt5_module.ORDER_TYPE_BUY if action == "BUY" else mt5_module.ORDER_TYPE_SELL,
                "price": float(price),
                "sl": float(sl),
                "tp": float(tp),
                "deviation": 40,
                "magic": MAGIC,
                "comment": "SV2",
                "type_time": mt5_module.ORDER_TIME_GTC,
                "type_filling": int(fill),
            }
            res = mt5_module.order_send(req)
            if res is None:
                journal({"ev": "order_send_none", "pair": p, "err": str(mt5_module.last_error())})
                continue
            rc = int(res.retcode)
            if rc == 10030:  # invalid filling -> try next mode
                continue
            break
        if res is None:
            continue
        rc = int(res.retcode)
        if rc in (10009, 10008, 10010):
            open_count += 1
            t = int(res.order)
            book[str(t)] = {"pair": p, "symbol": pair, "entry": float(res.price or price),
                            "stop_points": stop_dist, "be_moved": False,
                            "opened": f"{today} {hhmmss}"}
            executed.append({"Action": action, "Pair": p, "Ticket": t, "Vol": vol,
                             "Price": float(res.price or price), "SL": sl, "TP": tp})
            journal({"ev": "executed", "pair": p, "act": action, "retcode": rc, "ticket": t})
        elif rc == 10018:
            journal({"ev": "market_closed", "pair": p, "act": action})
        else:
            journal({"ev": "order_failed", "pair": p, "act": action, "retcode": rc,
                     "comment": str(res.comment) if res else "None"})

    if executed and mode != "paper" and cfg.get("alerts", {}).get("email_on_order", True):
        email_actions(executed, mode)

    # ---- daily summary (quiet: one per UTC day after summary hour) ----
    sum_hour = int(cfg.get("alerts", {}).get("daily_summary_utc_hour", 21))
    if (wday != 6 and hour >= sum_hour and state.get("summary_sent_day") != today):
        state["summary_sent_day"] = today
        send_email(
            f"📊 Sentinel v2 daily summary {today}",
            (f"<p>mode={mode} | equity={equity:.2f} | day_pnl={day_pnl * 100:.2f}%</p>"
             f"<p>open={len(live)} | "
             f"recent closes: <code>{json.dumps((state.get('closed_log') or [])[-6:], ensure_ascii=False)}</code></p>"),
        )

    summary = {"open_positions": len(live), "day_pnl_pct": round(day_pnl * 100, 3)}
    return summary


def atr_fallback(bs) -> float:
    from strategy import atr
    a = atr(bs, 14)
    return a[-1] if a else 0.0


def be_price(info, entry: float, direction: str):
    point = info.point
    digits = info.digits
    offset = 0.1 * point
    return round(entry + offset, digits) if direction == "BUY" else round(entry - offset, digits)


def main() -> int:
    try:
        from mt5_ipc_patch import apply as patch_apply
        ok = patch_apply()
        log(f"IPC patch applied={ok}")
    except Exception as exc:
        log(f"IPC patch skipped: {type(exc).__name__}: {exc}")

    cfg, halt = load_config()
    state = load_state()

    if halt:
        journal({"ev": "halt", "why": "config halt=true"})
        write_status(state, cfg, True, {"open_positions": 0})
        log("HALT: config.yml halt=true -> idle; chain re-arm will stop")
        return 0

    if str(cfg.get("mode", "paper")).lower() == "live":
        send_email("⚠️ Sentinel v2 LIVE MODE active",
                   "<p>This engine is configured for REAL-money trading. If this was not meant, set mode: demo in config.yml now.</p>")

    try:
        import MetaTrader5 as mt5_module
        summary = run_cycles(cfg, mt5_module, state, halt_bit=halt)
    except Exception as exc:
        traceback.print_exc()
        try:
            save_state(state)
        except Exception:
            pass
        email_critical("pipeline", exc)
        write_status(state, cfg, halt, {"open_positions": 0})
        return 1
    save_state(state)
    write_status(state, cfg, halt, summary)
    log("cycle complete; state + journal saved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
