#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EURUSD M15 trading bot — Exness MT5 (demo/trial) + Gemini 3.8 Flash (5-key rotation) + Resend alerts.

Designed for GitHub Actions (windows-latest) running every 15 minutes.

Key rotation (24/7 operation):
  - state.json persists {key_index, run_position, total_runs, ...} and is committed
    back to the repository by the workflow after every run.
  - Each 15-minute run consumes 1 slot on the active key; after 8 runs (2 hours)
    the engine rotates to the next key (wraps 5 -> 1, infinite loop).

Alert policy (Resend, onboarding@resend.dev -> RECEIVER_EMAIL):
  - ANY error at any stage (connection, LLM, execution, pipeline) -> error alert email
  - Successful order execution                             -> trade alert email
  - Successful boot verification (NOTIFY_BOOT=true run)    -> verification email
  - Everything else (HOLD, market closed, routine runs)    -> silent
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Configuration (secrets injected by GitHub Actions workflow)
# --------------------------------------------------------------------------- #
GEMINI_KEYS = []
for _i in (1, 2, 3, 4, 5):
    _k = os.environ.get(f"GEMINI_API_KEY_{_i}", "").strip()
    if _k:
        GEMINI_KEYS.append(_k)

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "").strip()
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "onboarding@resend.dev")

EXNESS_LOGIN = os.environ.get("EXNESS_LOGIN", "").strip()
EXNESS_PASSWORD = os.environ.get("EXNESS_PASSWORD", "")
EXNESS_SERVER = os.environ.get("EXNESS_SERVER", "").strip()

SYMBOL = os.environ.get("SYMBOL", "EURUSD").strip()
TIMEFRAME = os.environ.get("TIMEFRAME", "M15").strip()
CANDLE_COUNT = int(os.environ.get("CANDLE_COUNT", "20"))
LOT_SIZE = float(os.environ.get("LOT_SIZE", "0.01"))
MAGIC_NUMBER = int(os.environ.get("MAGIC_NUMBER", "884202"))
MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", "1"))
KEY_RUNS_PER_KEY = int(os.environ.get("KEY_RUNS_PER_KEY", "8"))
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()
GEMINI_FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get(
        "GEMINI_MODEL_FALLBACKS", "gemini-3.7-flash,gemini-3.5-flash,gemini-flash-latest"
    ).split(",")
    if m.strip()
]
NOTIFY_BOOT = os.environ.get("NOTIFY_BOOT", "false").strip().lower() in ("1", "true", "yes")
EMAIL_MARKET_CLOSED = os.environ.get("EMAIL_MARKET_CLOSED", "false").strip().lower() in ("1", "true", "yes")

MT5_INSTALL_PATHS = [
    r"C:\Program Files\MetaTrader 5\terminal64.exe",
    r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
    r"C:\Program Files\Exness MT5\terminal64.exe",
    r"C:\Program Files\Exness MetaTrader 5\terminal64.exe",
]

RUN_URL = "{}/{}/actions/runs/{}".format(
    os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/"),
    os.environ.get("GITHUB_REPOSITORY", ""),
    os.environ.get("GITHUB_RUN_ID", ""),
)

SYSTEM_INSTRUCTION = (
    'You are a disciplined EURUSD M15 scalping analyst. You receive the latest 20 OHLCV candles. '
    'You must reply ONLY with a single minified JSON object and nothing else, in exactly this schema: '
    '{"action": "BUY/SELL/HOLD", "sl_points": 150, "tp_points": 300}. '
    '"action" must be exactly one of BUY, SELL or HOLD. '
    '"sl_points" and "tp_points" are integer distances in MT5 points (EURUSD point = 0.00001): '
    "typical sl_points=150 (15 pips), tp_points=300 (30 pips); keep both in the range 60..600. "
    "Prefer HOLD unless the candle sequence clearly shows directional momentum or a strong reversal structure. "
    "Never wrap the JSON in markdown, never add explanations."
)

_mail_status = {"ok": None, "error": None}


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #
def log(*args) -> None:
    try:
        print(*args, flush=True)
    except UnicodeEncodeError:
        # Windows runner console can be cp1252; strip instead of crashing
        safe = " ".join(str(a).encode("ascii", "replace").decode("ascii") for a in args)
        print(safe, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def run_link() -> str:
    return f'<a href="{RUN_URL}">workflow run</a>' if RUN_URL.strip("/") != RUN_URL else ""


def send_email(subject: str, html: str) -> bool:
    """Sends an email through the Resend REST API. Returns True on success."""
    if not RESEND_API_KEY or not RECEIVER_EMAIL:
        log("EMAIL SKIPPED (missing RESEND_API_KEY or RECEIVER_EMAIL):", subject)
        _mail_status["error"] = "missing Resend credentials"
        return False
    payload = {
        "from": SENDER_EMAIL,
        "to": [RECEIVER_EMAIL],
        "subject": subject,
        "html": html,
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
            # Resend's Cloudflare front bans the default "Python-urllib/x" signature (403, error 1010)
            "User-Agent": "resend-python/2.0.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
        log(f"EMAIL SENT: {subject} -> {body[:120]}")
        _mail_status["ok"] = subject
        return True
    except Exception as exc:  # notification failure must never kill the run
        body = ""
        if isinstance(exc, urllib.error.HTTPError):
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
        log(f"EMAIL FAILED: {type(exc).__name__}: {exc} {body[:300]}")
        _mail_status["error"] = f"{type(exc).__name__}: {exc} {body[:300]}"
        return False


# --------------------------------------------------------------------------- #
# Rotation state
# --------------------------------------------------------------------------- #
DEFAULT_STATE = {
    "key_index": 0,
    "run_position": 0,
    "total_runs": 0,
    "last_run_utc": None,
    "last_run_result": None,
}


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if isinstance(state, dict):
            merged = dict(DEFAULT_STATE)
            merged.update(state)
            return merged
    except Exception as exc:
        log(f"state.json not usable ({type(exc).__name__}: {exc}); starting fresh")
    return dict(DEFAULT_STATE)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)
        fh.write("\n")


def rotate_state(state: dict, summary) -> dict:
    """Consumes one 15-minute slot on the active key; rotates after 8 slots."""
    new = dict(state)
    new["run_position"] = int(new.get("run_position", 0)) + 1
    new["total_runs"] = int(new.get("total_runs", 0)) + 1
    new["last_run_utc"] = utc_now()
    new["last_run_result"] = summary
    if new["run_position"] >= KEY_RUNS_PER_KEY:
        new["run_position"] = 0
        new["key_index"] = (int(new.get("key_index", 0)) + 1) % len(GEMINI_KEYS)
    save_state(new)
    return new


# --------------------------------------------------------------------------- #
# MetaTrader 5 layer
# --------------------------------------------------------------------------- #
def kill_terminals() -> None:
    import subprocess
    subprocess.run(["taskkill", "/f", "/im", "terminal64.exe"], capture_output=True, shell=False)


def terminal_running() -> bool:
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq terminal64.exe"],
            capture_output=True, shell=False,
        ).stdout.decode("utf-8", "replace")
        return "terminal64.exe" in out
    except Exception:
        return False


def find_terminal_exe():
    # Workflow exports the actually-installed path via MT5_PATH (GITHUB_ENV)
    env_path = os.environ.get("MT5_PATH", "").strip()
    if env_path and os.path.exists(env_path):
        return env_path
    for path in MT5_INSTALL_PATHS:
        if os.path.exists(path):
            return path
    return None


def write_login_config(path=None) -> str:
    """MT5 startup config with auto-login.

    Uses the official [Common] keys (Login/Password/Server/ProxyEnable/
    NewsEnable/KeepPrivate) with CRLF line endings -- Windows INI parsers
    ignore bare-LF files, which silently skips the auto-login.
    """
    ini = (
        "[Common]\r\n"
        f"Login={EXNESS_LOGIN}\r\n"
        f"Password={EXNESS_PASSWORD}\r\n"
        f"Server={EXNESS_SERVER}\r\n"
        "ProxyEnable=0\r\n"
        "CertInstall=0\r\n"
        "NewsEnable=0\r\n"
        "KeepPrivate=1\r\n"
        "\r\n"
        "[Experts]\r\n"
        "AllowLiveTrading=1\r\n"
        "AllowDllImport=0\r\n"
        "Enabled=1\r\n"
        "Account=0\r\n"
        "Profile=0\r\n"
    )
    if path is None:
        path = os.path.join(os.environ.get("RUNNER_TEMP") or os.getcwd(), "mt5login.ini")
    with open(path, "w", encoding="ascii", errors="replace", newline="") as fh:
        fh.write(ini)
    return path


def write_data_folder_config():
    """Also write the login config into the terminal data folder's own
    config/common.ini (the DEFAULT config the terminal reads even without
    /config:). Returns the data-folder path or None."""
    import glob
    base = os.path.join(os.environ.get("APPDATA", ""), "MetaQuotes", "Terminal")
    for d in glob.glob(os.path.join(base, "*")):
        if os.path.isdir(os.path.join(d, "config")):
            try:
                write_login_config(os.path.join(d, "config", "common.ini"))
                log(f"data-folder common.ini written: {d}")
                return d
            except Exception as exc:
                log(f"data-folder common.ini write failed ({exc})")
    return None


def launch_terminal(config_path=None, portable=False) -> None:
    import subprocess
    exe = find_terminal_exe()
    if exe is None:
        raise RuntimeError("terminal64.exe not found in any known install path")
    args = [exe]
    if config_path:
        args.append(f"/config:{config_path}")
    if portable:
        args.append("/portable")
    log(f"launching terminal: {' '.join(args)}")
    subprocess.Popen(args, cwd=os.path.dirname(exe))


def _read_mt5_log(fp: str) -> str:
    """Terminal journals are UTF-16LE (usually with BOM); decode correctly."""
    raw = open(fp, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    sample = raw[:512]
    if sample and sample.count(0) > len(sample) * 0.3:
        return raw.decode("utf-16-le", "replace")
    return raw.decode("utf-8", "replace")


def dump_terminal_logs(tail: int = 60) -> None:
    """Print the tail of the terminal's own journal (skips metaeditor logs)."""
    base = os.path.join(os.environ.get("APPDATA", ""), "MetaQuotes", "Terminal")
    if not os.path.isdir(base):
        log("no terminal data dir found")
        return
    for data_dir in sorted(os.listdir(base)):
        logdir = os.path.join(base, data_dir, "logs")
        if not os.path.isdir(logdir):
            continue
        dumped = 0
        for name in sorted(os.listdir(logdir), reverse=True):
            if dumped >= 1:
                break
            # main journal files are named like 20261028.log
            if not (name.startswith("20") and name.endswith(".log")):
                continue
            fp = os.path.join(logdir, name)
            try:
                if os.path.getsize(fp) == 0:
                    continue
                text = _read_mt5_log(fp)
                lines = [l.rstrip() for l in text.splitlines() if l.strip()]
                log(f"--- terminal journal {data_dir}/{name} (tail {tail}) ---")
                for line in lines[-tail:]:
                    log("  " + line)
                dumped += 1
            except Exception as exc:
                log(f"  (log read failed: {exc})")


def wait_ready(mt5_module, seconds: int = 90):
    """Polls until the terminal is genuinely connected to the broker.

    Returns the account_info() namedtuple on success, None on timeout.
    Logs a compact snapshot of terminal/account state on every change so CI
    failures are diagnosable.
    """
    deadline = time.time() + seconds
    last_snapshot = None
    term = acct = None
    while time.time() < deadline:
        term = mt5_module.terminal_info()
        acct = mt5_module.account_info()
        if term is not None and acct is not None and getattr(term, "connected", False):
            log(
                f"MT5 CONNECTED: login={acct.login} server={acct.server} "
                f"account={acct.name} balance={acct.balance} {acct.currency}"
            )
            return acct
        snapshot = (
            f"terminal_info={'None' if term is None else 'present'} "
            f"connected={getattr(term, 'connected', None)} "
            f"trade_allowed={getattr(term, 'trade_allowed', None)} "
            f"account_info={'None' if acct is None else f'login={acct.login} server={acct.server}'}"
        )
        if snapshot != last_snapshot:
            log(f"  wait_ready: {snapshot}")
            last_snapshot = snapshot
        time.sleep(3)
    log(f"  wait_ready TIMEOUT after {seconds}s: terminal_info={term!r} account_info={acct!r}")
    return None


def connect_mt5(mt5_module) -> dict:
    if not EXNESS_LOGIN or not EXNESS_PASSWORD or not EXNESS_SERVER:
        raise RuntimeError("EXNESS_LOGIN / EXNESS_PASSWORD / EXNESS_SERVER env missing")

    try:
        login_int = int(EXNESS_LOGIN)
    except ValueError as exc:
        raise RuntimeError(f"EXNESS_LOGIN must be numeric, got {EXNESS_LOGIN!r}") from exc

    creds = {"login": login_int, "password": EXNESS_PASSWORD, "server": EXNESS_SERVER}

    strategies = [
        # Let the module spawn terminal64.exe itself (same elevation/session,
        # no first-run wizard) and hand it the credentials directly.
        ("module-spawned terminal with credentials (patched IPC)", "spawn"),
        # Fallbacks: launch with a startup config and attach.
        ("config-file launch (auto-login) + attach", "config"),
        ("portable config-file launch + attach", "config_portable"),
    ]

    kill_terminals()
    time.sleep(3)
    last_err = None
    for name, mode in strategies:
        portable = mode.endswith("_portable")
        log(f"MT5 strategy: {name}")
        try:
            if mode == "spawn":
                exe = find_terminal_exe()
                if exe is None:
                    raise RuntimeError("terminal64.exe not found in any known install path")
                # timeout is in MILLISECONDS (official docs: default 60000 = 60s)
                ok = mt5_module.initialize(path=exe, timeout=90000, **creds)
            elif mode.startswith("config"):
                write_data_folder_config()  # also seed the terminal's default common.ini
                launch_terminal(write_login_config(), portable=portable)
                time.sleep(30)
                ok = (
                    mt5_module.initialize(portable=True, timeout=90000)
                    if portable
                    else mt5_module.initialize(timeout=90000)
                )
            else:
                ok = mt5_module.initialize(timeout=90000, **creds)
            if ok:
                acct = wait_ready(mt5_module, 90)
                if acct is None:
                    # Attached but no broker connection yet -> explicit login
                    log("  -> attach OK but not connected; explicit mt5.login()...")
                    login_ok = mt5_module.login(login_int, password=EXNESS_PASSWORD, server=EXNESS_SERVER)
                    log(f"  -> mt5.login() returned {login_ok}, last_error={mt5_module.last_error()}")
                    if login_ok:
                        acct = wait_ready(mt5_module, 60)
                if acct is not None:
                    return {"login": acct.login, "server": acct.server, "balance": acct.balance}
                last_err = f"initialized but no account/connected ({mt5_module.last_error()})"
            else:
                last_err = str(mt5_module.last_error())
            log(f"  -> {last_err}")
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            log(f"  -> strategy error: {last_err}")
        try:
            mt5_module.shutdown()
        except Exception:
            pass
        dump_terminal_logs()
        kill_terminals()
        time.sleep(10)
    raise RuntimeError(f"MT5 connection failed after all strategies; last error: {last_err}")


def resolve_timeframe(mt5_module):
    tf = getattr(mt5_module, f"TIMEFRAME_{TIMEFRAME}", None)
    if tf is None:
        raise RuntimeError(f"Unsupported TIMEFRAME '{TIMEFRAME}'")
    return tf


def fetch_rates(mt5_module, timeframe):
    if not mt5_module.symbol_select(SYMBOL, True):
        raise RuntimeError(f"symbol_select({SYMBOL}) failed: {mt5_module.last_error()}")
    for attempt in range(1, 6):
        rates = mt5_module.copy_rates_from_pos(SYMBOL, timeframe, 0, CANDLE_COUNT)
        if rates is not None and len(rates) >= 10:
            return rates
        log(f"rates attempt {attempt}/5 empty ({mt5_module.last_error()}), retrying...")
        time.sleep(10)
    raise RuntimeError(f"copy_rates_from_pos failed for {SYMBOL} {TIMEFRAME}: {mt5_module.last_error()}")


def build_prompt(symbol_info, tick, rates) -> str:
    lines = []
    for row in rates:
        dt = datetime.fromtimestamp(int(row["time"]), tz=timezone.utc)
        lines.append(
            f"{dt:%Y-%m-%d %H:%M}  O={float(row['open']):.5f}  H={float(row['high']):.5f}  "
            f"L={float(row['low']):.5f}  C={float(row['close']):.5f}  V={int(row['tick_volume'])}"
        )
    candles_text = "\n".join(lines)
    spread_pts = 0
    if tick is not None and symbol_info.point:
        spread_pts = (tick.ask - tick.bid) / symbol_info.point
    return (
        f"Market snapshot for {SYMBOL} on {TIMEFRAME} at {utc_now()} UTC.\n"
        f"Current spread: {spread_pts:.0f} points. Point size: {symbol_info.point}.\n\n"
        f"Latest {len(rates)} candles (UTC, oldest -> newest):\n{candles_text}\n\n"
        "Decide the next action for a NEW position on this symbol. "
        'Respond ONLY with JSON: {"action": "BUY/SELL/HOLD", "sl_points": <int>, "tp_points": <int>}.'
    )


# --------------------------------------------------------------------------- #
# Gemini layer (5-key rotation engine)
# --------------------------------------------------------------------------- #
def parse_signal(text: str) -> dict:
    raw = text.strip()
    if "```" in raw:  # strip potential markdown fences
        blocks = raw.split("```")
        raw = max(blocks, key=len)
        if raw[:4].lower() == "json":
            raw = raw[4:].lstrip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no JSON object found in model output: {text[:200]!r}")
    data = json.loads(raw[start : end + 1])
    action = str(data.get("action", "")).strip().upper()
    if action not in ("BUY", "SELL", "HOLD"):
        raise ValueError(f"invalid action {action!r} in model output")
    sl = int(data.get("sl_points", 150))
    tp = int(data.get("tp_points", 300))
    sl = max(60, min(600, sl))
    tp = max(60, min(600, tp))
    return {"action": action, "sl_points": sl, "tp_points": tp}


def call_gemini(key_index: int, prompt: str) -> dict:
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:
        raise RuntimeError(f"google-genai import failed: {exc}") from exc

    models = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACK_MODELS if m != GEMINI_MODEL]
    attempts = []
    for model in models:
        for offset in (0, 1, 2):  # active key + next two keys (transient 429/5xx fallback)
            key = GEMINI_KEYS[(key_index + offset) % len(GEMINI_KEYS)]
            attempts.append((model, key, (key_index + offset) % len(GEMINI_KEYS)))

    errors = []
    for model, key, slot in attempts:
        tag = f"model={model} key#{slot + 1}"
        try:
            client = genai.Client(api_key=key)
            config = types.GenerateContentConfig(
                temperature=0.1,
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",  # force strict JSON
            )
            resp = client.models.generate_content(model=model, contents=prompt, config=config)
            text = ""
            try:
                text = (resp.text or "").strip()
            except Exception:
                text = ""
            if not text:
                feedback = getattr(resp, "prompt_feedback", None)
                raise RuntimeError(f"empty model response (blocked?) feedback={feedback}")
            signal = parse_signal(text)
            log(f"GEMINI OK via {tag}: {signal}")
            return {"signal": signal, "model": model, "key_slot": slot + 1}
        except Exception as exc:
            err = f"{tag}: {type(exc).__name__}: {str(exc)[:400]}"
            errors.append(err)
            log(f"GEMINI attempt failed -> {err}")
            time.sleep(5)
    raise RuntimeError("All Gemini attempts failed:\n" + "\n".join(errors))


# --------------------------------------------------------------------------- #
# Order execution layer
# --------------------------------------------------------------------------- #
def execute_trade(mt5_module, symbol_info, signal: dict):
    action = signal["action"]
    if action == "HOLD":
        log("SIGNAL HOLD -> no order sent")
        return None

    open_positions = mt5_module.positions_get(symbol=SYMBOL) or []
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        log(
            f"{len(open_positions)} open position(s) on {SYMBOL} >= MAX_OPEN_POSITIONS="
            f"{MAX_OPEN_POSITIONS} -> signal ignored"
        )
        return {"skipped": "open position limit", "open_positions": len(open_positions)}

    for _ in range(5):
        tick = mt5_module.symbol_info_tick(SYMBOL)
        if tick is not None and tick.ask > 0 and tick.bid > 0:
            break
        time.sleep(2)
    if tick is None or tick.ask <= 0:
        raise RuntimeError(f"no valid tick for {SYMBOL}")

    point = symbol_info.point
    digits = symbol_info.digits
    stops_pts = int(getattr(symbol_info, "trade_stops_level", 0) or 0)
    sl_pts = max(int(signal["sl_points"]), stops_pts + 2)
    tp_pts = max(int(signal["tp_points"]), stops_pts + 2)

    if action == "BUY":
        order_type = mt5_module.ORDER_TYPE_BUY
        price = tick.ask
        sl = round(price - sl_pts * point, digits)
        tp = round(price + tp_pts * point, digits)
    else:
        order_type = mt5_module.ORDER_TYPE_SELL
        price = tick.bid
        sl = round(price + sl_pts * point, digits)
        tp = round(price - tp_pts * point, digits)

    volume = LOT_SIZE
    try:
        step = float(symbol_info.volume_step or 0.01)
        vmin = float(symbol_info.volume_min or 0.01)
        vmax = float(symbol_info.volume_max or 100.0)
        volume = min(max(round(round(volume / step) * step, 8), vmin), vmax)
    except Exception:
        pass

    # Choose allowed filling mode from the symbol's bitmask, with brute-force fallbacks
    filling = []
    try:
        fm = int(getattr(symbol_info, "filling_mode", 0) or 0)
        if fm & 1:
            filling.append(mt5_module.ORDER_FILLING_FOK)
        if fm & 2:
            filling.append(mt5_module.ORDER_FILLING_IOC)
    except Exception:
        pass
    filling += [mt5_module.ORDER_FILLING_FOK, mt5_module.ORDER_FILLING_IOC, mt5_module.ORDER_FILLING_RETURN]
    seen = set()
    filling = [f for f in filling if not (f in seen or seen.add(f))]

    retcode_done = getattr(mt5_module, "TRADE_RETCODE_DONE", 10009)
    retcode_part = getattr(mt5_module, "TRADE_RETCODE_DONE_PARTIAL", 10010)
    retcode_placed = getattr(mt5_module, "TRADE_RETCODE_PLACED", 10008)
    retcode_invalid_fill = getattr(mt5_module, "TRADE_RETCODE_INVALID_FILL", 10030)
    retcode_market_closed = getattr(mt5_module, "TRADE_RETCODE_MARKET_CLOSED", 10018)

    request = {
        "action": mt5_module.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 30,
        "magic": MAGIC_NUMBER,
        "comment": "gemini-3.8-flash m15",
        "type_time": getattr(mt5_module, "ORDER_TIME_GTC", 2),
        "type_filling": filling[0],
    }

    result = None
    for mode in filling:
        request["type_filling"] = mode
        result = mt5_module.order_send(request)
        if result is None:
            raise RuntimeError(f"order_send returned None: {mt5_module.last_error()}")
        if result.retcode in (retcode_done, retcode_placed, retcode_part):
            break
        if result.retcode != retcode_invalid_fill:
            break
    else:
        pass

    if result.retcode == retcode_market_closed:
        log("MARKET CLOSED -> trading session unavailable; signal ignored (no alert by design)")
        return {"skipped": "market_closed"}

    if result.retcode not in (retcode_done, retcode_placed, retcode_part):
        raise RuntimeError(
            f"order_send FAILED retcode={result.retcode} comment='{result.comment}'"
            f" request={json.dumps({k: (str(v) if k == 'type_filling' else v) for k, v in request.items()})}"
        )

    trade_info = {
        "ticket": int(result.order),
        "action": action,
        "volume": float(result.volume if result.volume else volume),
        "price": float(result.price if result.price else price),
        "sl": float(sl),
        "tp": float(tp),
        "retcode": int(result.retcode),
        "comment": str(result.comment or ""),
    }
    log(f"ORDER EXECUTED: {trade_info}")
    return trade_info


# --------------------------------------------------------------------------- #
# Emails
# --------------------------------------------------------------------------- #
def fmt_trade_table(trade: dict) -> str:
    rows = "".join(
        f"<tr><td style='padding:4px 10px'>{k}</td><td style='padding:4px 10px'><b>{v}</b></td></tr>"
        for k, v in trade.items()
    )
    return f"<table border='1' style='border-collapse:collapse'>{rows}</table>"


def email_trade(signal: dict, trade: dict) -> bool:
    return send_email(
        f"✅ Trading bot: {trade['action']} order executed on {SYMBOL}",
        (
            "<h2>📈 Order executed successfully</h2>"
            f"<p><b>{trade['action']}</b> {trade['volume']} lots {SYMBOL} ({TIMEFRAME})</p>"
            f"<p>Signal: <code>{json.dumps(signal)}</code></p>"
            f"{fmt_trade_table(trade)}"
            f"<p>UTC time: {utc_now()}</p><p>{run_link()}</p>"
        ),
    )


def email_boot(signal: dict, gemini_meta: dict) -> bool:
    subject = "✅ Trading bot VERIFIED & RUNNING (Exness MT5 + Gemini 3.8 Flash)"
    html = (
        "<h2>🚀 Boot verification completed successfully</h2>"
        "<ul>"
        "<li>✔ MT5 terminal connection to broker: OK</li>"
        f"<li>✔ {SYMBOL} {TIMEFRAME}: {CANDLE_COUNT} candles fetched: OK</li>"
        "<li>✔ Gemini inference (5-key rotation engine): OK</li>"
        f"<li>Active key slot: key #{gemini_meta['key_slot']} of 5</li>"
        f"<li>✔ Model used: {gemini_meta['model']}</li>"
        f"<li>Latest signal: <code>{json.dumps(signal)}</code></li>"
        "</ul>"
        "<p>The bot is now deployed and will run automatically every 15 minutes (96 runs/day, "
        "8 runs per key slot, rotating across your 5 Gemini keys for 24/7 free-tier operation).</p>"
        "<p><b>Alert policy:</b> you will receive an email ONLY on errors, on executed trades, "
        "or on verification events like this one.</p>"
        f"<p>UTC time: {utc_now()} — {run_link()}</p>"
    )
    return send_email(subject, html)


def email_error(stage: str, exc: Exception, extra: str = "") -> bool:
    tb = traceback.format_exc(limit=8)
    return send_email(
        f"🚨 Trading bot ERROR: {type(exc).__name__}",
        (
            f"<h2>Error at stage: {stage}</h2>"
            f"<p><b>{type(exc).__name__}</b>: {str(exc)[:1200]}</p>"
            f"<pre style='white-space:pre-wrap;background:#f6f6f6;padding:10px'>{tb[:3000]}</pre>"
            f"<p>{extra}</p>"
            f"<p>UTC time: {utc_now()} — {run_link()}</p>"
        ),
    )


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #
def run_pipeline(state: dict) -> dict:
    # The IPC patch MUST be applied before the first mt5.initialize() call --
    # it forces the terminal-status reply past the STATUS_INITIALIZING deadlock
    # that otherwise makes initialize() time out on fresh CI terminals
    # (see mt5_ipc_patch.py for details).
    try:
        import mt5_ipc_patch
        log("IPC patch module imported OK; calling apply()...")
        _patched = mt5_ipc_patch.apply()
        log(f"IPC patch: applied={_patched} | {mt5_ipc_patch.status}")
    except Exception as exc:
        log(f"IPC patch not applied: {type(exc).__name__}: {exc}")
    log("importing MetaTrader5...")
    import MetaTrader5 as mt5_module
    log("MetaTrader5 imported OK")

    key_index = int(state.get("key_index", 0)) % len(GEMINI_KEYS)
    run_position = int(state.get("run_position", 0))
    log(
        f"=== run #{int(state.get('total_runs', 0)) + 1} | key slot #{key_index + 1}/"
        f"{len(GEMINI_KEYS)} | slot position {run_position + 1}/{KEY_RUNS_PER_KEY} ==="
    )

    mt5_module.shutdown()  # ensure clean start on reused terminals
    acct = connect_mt5(mt5_module)

    timeframe = resolve_timeframe(mt5_module)
    rates = fetch_rates(mt5_module, timeframe)
    symbol_info = mt5_module.symbol_info(SYMBOL)
    if symbol_info is None:
        raise RuntimeError(f"symbol_info({SYMBOL}) is None: {mt5_module.last_error()}")
    tick = mt5_module.symbol_info_tick(SYMBOL)
    prompt = build_prompt(symbol_info, tick, rates)

    gemini_meta = call_gemini(key_index, prompt)
    signal = gemini_meta["signal"]

    trade = execute_trade(mt5_module, symbol_info, signal)

    mt5_module.shutdown()
    return {
        "account": acct,
        "gemini": gemini_meta,
        "signal": signal,
        "trade": trade,
        "utc": utc_now(),
    }


def main() -> int:
    force_utf8_stdio()
    log(f"bot starting (python {sys.version.split()[0]}, {sys.platform})")
    if not GEMINI_KEYS:
        send_email(
            "🚨 Trading bot ERROR: no Gemini keys",
            "<p>GEMINI_API_KEY_1..5 environment variables are missing — cannot operate.</p>"
            f"<p>{run_link()}</p>",
        )
        return 2

    state = load_state()
    summary = None
    error = None
    try:
        summary = run_pipeline(state)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        try:
            rotate_state(
                state,
                summary={"status": "ok" if summary else "error", "utc": utc_now()}
                if summary is None
                else {k: (v if isinstance(v, (int, float, str, type(None))) else str(v)) for k, v in summary.items()},
            )
            log(f"state rotated & saved (next slot: key #{state_wait_placeholder(state)} position)")
        except Exception:
            traceback.print_exc()

    # ---- alert policy ----
    if error is not None:
        email_error("pipeline", error, f"Account: {EXNESS_LOGIN} / {EXNESS_SERVER}; symbol {SYMBOL} {TIMEFRAME}")
        sys.exit(1)

    trade = summary.get("trade")
    if isinstance(trade, dict) and trade.get("ticket"):
        email_trade(summary["signal"], trade)

    if NOTIFY_BOOT:
        if not email_boot(summary["signal"], summary["gemini"]):
            log("BOOT VERIFICATION EMAIL FAILED -> marking run as failure")
            return 1
        log("Boot verification completed and notification dispatched.")

    return 0


def state_wait_placeholder(state: dict) -> int:
    # small helper so the log above matches the rotated file
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return int(json.load(fh).get("key_index", 0)) + 1
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
