# The Impossible Mission — Auto-Trading Bot

**Exness MT5 (demo/trial) + Gemini 3.8 Flash (5-key rotation) + Resend alerts, running 24/7 for free on GitHub Actions.**

## How it works

- Runs every **15 minutes** on `windows-latest` via [`.github/workflows/trading.yml`](.github/workflows/trading.yml).
- Each run installs an MT5 terminal, connects to `Exness-MT5Trial15`, fetches the latest **20 M15 candles** for **EURUSD**, asks **`gemini-3.8-flash`** (temperature 0.1) for a strict-JSON signal
  `{"action": "BUY/SELL/HOLD", "sl_points": 150, "tp_points": 300}`,
  and places the order through `mt5.order_send` with broker-side SL/TP.
- **5-key rotation engine**: `state.json` is committed back to this repo after every run,
  so the engine knows which Gemini key slot is active (8 runs = 2 hours per key, then rotates 1→2→3→4→5→1… forever).
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
