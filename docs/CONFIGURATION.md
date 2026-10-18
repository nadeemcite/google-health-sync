# Configuration reference

All settings are environment variables, read from `.env` (Docker passes them through `env_file`). [`.env.example`](../.env.example) has the same list with comments. Hour ranges are on the 24h clock, written `start-end`, and may wrap past midnight (`22-06`, `03-04`).

## Google

| Variable | Default | Meaning |
|---|---|---|
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | required | Your Desktop OAuth client |
| `GOOGLE_PROJECT_NUMBER` | | Only for `healthsync.py subscribe` (webhooks) |
| `SUBSCRIBER_ID` | `health-sync` | Name of the webhook subscription |
| `DEVICE_NAME` | any Fitbit/Pixel wearable | Only use data from this device, as shown in the report header |

## Your day (drives reports, the Diet tab and the assistant)

| Variable | Default | Meaning |
|---|---|---|
| `TZ` | `UTC` | Timezone for the scheduler and "today" |
| `TARGET_BEDTIME` | `23-00` | You fall asleep within this range. The *sleep window* (when the Diet tab says "avoid eating") starts when it ends |
| `TARGET_WAKE` | `07-08` | You wake within this range. The sleep window ends when it starts |
| `WORK_HOURS` | none | Optional, e.g. `09-17` or `18-03`. Enables shift-aware advice |
| `DAYS_OFF` | `Sat,Sun` | Day-off nights are shown separately in the report |
| `WEIGHT_KG`, `HEIGHT_CM` | none | Starting values only. Log in the app, or let Google Health supply them |

## Assistant

| Variable | Default | Meaning |
|---|---|---|
| `ASSISTANT_MODEL` | `ollama_chat/glm-5.3-flash` | A [LiteLLM](https://docs.litellm.ai/docs/providers) `provider/model` string |
| `MEMORY_MODEL` | same as `ASSISTANT_MODEL` | Model that rewrites `MEMORY.md` after each exchange. A smaller one is fine |
| `ASSISTANT_REASONING` | `low` | `low`, `medium` or `high`. Reasoning models think before answering; `low` keeps replies quick |
| `OLLAMA_API_BASE`, `OLLAMA_API_KEY` | | Ollama Cloud (`https://ollama.com`) or a local Ollama URL |
| `OPENAI_API_KEY`, `GEMINI_API_KEY`, ... | | Whatever your chosen provider needs; LiteLLM reads them directly |
| `MEMORY_FILE` | `data/MEMORY.md` | Where the assistant's memory lives |

## Schedule

| Variable | Default | Meaning |
|---|---|---|
| `CATCHUP_HOUR` | `10` | Hour (local) of the daily catch-up. Pick about 2 h after you usually wake so last night's sleep is processed |
| `POLL_EVERY_HOURS` | none | Also poll every N hours (useful without webhooks) |

Fixed in code (`healthsync.py`): webhook worker every 15 min, Diet tab refresh about every 55 min, 48 h overlap on each catch-up, 30-day first backfill, weekly report on Mondays.

## Web app

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `18787` | Listen port inside the container |
| `REPORT_BASIC_AUTH` | off | `true` requires HTTP basic auth for the whole app (not `/healthz` or `/webhook`) |
| `REPORT_USER`, `REPORT_PASSWORD` | `me`, none | The basic-auth login. With the flag on and no password the app answers 404 rather than opening up |
| `REPORT_DIR` | `reports` | Where PDFs are written |
| `HEALTH_DB` | `data/health.db` | SQLite file |
| `CHROME_NO_SANDBOX` | off | Set to `1` if Chromium aborts on your host (some AppArmor setups) |

## Webhooks and alerts

| Variable | Meaning |
|---|---|
| `WEBHOOK_URL` | Public HTTPS URL ending in `/webhook` |
| `WEBHOOK_SECRET` | Shared secret (`openssl rand -hex 32`). Empty means polling-only |
| `NTFY_TOPIC` | Push alerts to `https://ntfy.sh/<topic>` when a job fails |
| `HEARTBEAT_URL` | Dead-man's switch pinged after each daily sync, e.g. a healthchecks.io URL |
