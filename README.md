# The Impossible Mission — Auto-Trading Bot

**Exness MT5 (demo/trial) + Gemini 3.8 Flash (5-key rotation) + Resend alerts, running 24/7 for free on GitHub Actions.**

## How it works

- Runs every **~15 minutes** on `windows-latest` via [`.github/workflows/trading.yml`](.github/workflows/trading.yml).
- **Scheduling = self-perpetuating chain**: GitHub's native cron scheduler is unreliable
  ([actions/runner#4210](https://github.com/actions/runner/issues/4210) — scheduled runs can silently never fire).
  Instead, every run **re-arms the next run itself**: a step right after checkout waits ~8 minutes and then calls the
  `workflow_dispatch` API, so the chain continues 24/7 independently of GitHub's scheduler. The step is placed *before*
  the fragile MT5 install (a crashed run still leaves its successor queued) and includes chain de-duplication
  (if another run is already queued — cron, an external scheduler ping, or a manual test — it becomes the next link,
  so there is always exactly one chain). The `cron` trigger in the workflow is kept only as a chain-restarter watchdog.
- Each run installs an MT5 terminal, connects to `Exness-MT5Trial15`, fetches the latest **20 M15 candles** for **EURUSD**, asks **`gemini-3.8-flash`** (temperature 0.1) for a strict-JSON signal
  `{"action": "BUY/SELL/HOLD", "sl_points": 150, "tp_points": 300}`,
  and places the order through `mt5.order_send` with broker-side SL/TP.
- **Symbol auto-resolution**: Exness names the instrument `EURUSDm` (note the suffix). The bot calls `symbols_total()`/`symbols_get()` and automatically resolves and uses the broker-specific name — no manual config needed if the broker renames symbols.
- **5-key rotation engine**: `state.json` is committed back to this repo after every run
  (race-proof push: rebase onto latest master, conflicts resolved in favor of the fresher state, 5 attempts),
  so the engine knows which Gemini key slot is active (8 runs = ~2 hours per key, then rotates 1→2→3→4→5→1… forever).
- **Alert policy (Resend, `onboarding@resend.dev`)**: email is sent **only** on errors (any stage), on successful order executions, and on successful boot verification. Routine/quiet runs stay silent. Market-closed responses are logged, not emailed, to avoid weekend spam.

## Repository secrets (set via GitHub → Settings → Secrets → Actions)

| Secret | Purpose |
|---|---|
| `GEMINI_API_KEY_1..5` | The Gemini 5-key rotation matrix |
| `RESEND_API_KEY` | Notification email API |
| `RECEIVER_EMAIL` | Where alerts are sent |
| `EXNESS_LOGIN` / `EXNESS_PASSWORD` / `EXNESS_SERVER` | MT5 demo account credentials |
| `DEPLOY_PAT` | PAT used to commit `state.json` (keeps the repo "active" so GitHub never auto-disables the schedule) |

## Manual test

Run the workflow *Trading Bot — EURUSD M15* from the **Actions** tab with **notify_boot** checked.
On full success you get the "verified & running" email; on any failure you get an error alert.

> ⚠️ Disclaimer: This bot executes LLM-generated signals on a **demo/trial** account.
> LLM output is not reliable financial advice — do not attach a live account without extensive forward-testing.
