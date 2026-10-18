# Tutorial: run your own copy

This takes you from nothing to a running health assistant on your own machine, using **your own Google Cloud project, your own watch data and your own model**. Nothing about the original author's setup is needed or reachable.

**Time:** about 45 minutes, most of it clicking through Google Cloud once.
**Cost:** Google Cloud and the Google Health API are free at this volume. The model depends on what you pick (see step 4). A local Ollama is free.

**What you will have at the end:** the app at `http://localhost:18787` with a chat assistant, a Diet tab and a weekly report, syncing your Fitbit / Pixel Watch data automatically.

> The Google Cloud console changes its labels from time to time. If a button is named slightly differently, look for the closest match. The [Google Health API docs](https://developers.google.com/health) are authoritative.

---

## 0. Before you start

You need:

| | |
|---|---|
| A **Fitbit or Pixel Watch** that syncs to the **Google Health app** (formerly the Fitbit app) | The data source. No watch? Skip to [demo mode](../README.md#try-it-with-demo-data). |
| A **Google account** | The one your watch data lives in. |
| **Docker** | `docker compose` must work. [Install Docker](https://docs.docker.com/get-docker/). |
| **[uv](https://docs.astral.sh/uv/)** and a **browser** on this machine | For the one-time Google sign-in. `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| A way to call an LLM | A key for Ollama Cloud, OpenAI, Gemini..., or a local [Ollama](https://ollama.com). See step 4. |

> **Good to know:** setup is self-serve through Google Cloud, and your own account works as a test user. An unverified app is limited to 100 users, which is plenty for personal use. Making it public would need Google's app verification and a security review, because every Google Health scope is classified Restricted. Access rules are Google's and can change, so check the [Google Health API docs](https://developers.google.com/health) if step 1 is blocked for you.

## 1. Google Cloud project

1. Open the [Google Cloud Console](https://console.cloud.google.com) and **create a project** (any name, e.g. `my-health`).
2. **APIs & Services → Library**, search **Google Health API** (`health.googleapis.com`) and **Enable** it.
3. **Google Auth Platform → Branding / Audience:**
   - User type: **External**.
   - Add your own Google account under **Test users**.
4. **Data Access (scopes):** add these four, all read-only:
   ```
   https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly
   https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly
   https://www.googleapis.com/auth/googlehealth.sleep.readonly
   https://www.googleapis.com/auth/googlehealth.profile.readonly
   ```
5. **Clients → Create client → Application type: Desktop app.** Copy the **Client ID** and **Client secret**.
6. **Audience → Publish app.** This matters: while the app is in *Testing*, Google expires refresh tokens after **7 days** and unattended syncing breaks. A personal app can stay *unverified*; you will click through a warning once at sign-in.
7. Note your **project number** (Console dashboard). You only need it for webhooks (step 8).

## 2. Get the code and configure it

```sh
git clone https://github.com/nadeemcite/google-health-sync.git
cd google-health-sync
cp .env.example .env
```

Edit `.env`. The minimum:

```ini
GOOGLE_CLIENT_ID=<from step 1.5>
GOOGLE_CLIENT_SECRET=<from step 1.5>

TZ=Europe/London                 # your timezone

# Your day, as hour ranges on the 24h clock. Overnight is fine.
TARGET_BEDTIME=23-00             # you fall asleep during this range
TARGET_WAKE=07-08                # you wake during this range
WORK_HOURS=09-17                 # optional. Night shift? 22-06
DAYS_OFF=Sat,Sun
CATCHUP_HOUR=10                  # daily sync, ~2h after you usually wake
```

These hours drive everything: the sleep chart targets, the Diet tab's "avoid eating now" window, meal times, and the assistant's advice. A night-shift worker would use e.g. `WORK_HOURS=22-06`, `TARGET_BEDTIME=07-08`, `TARGET_WAKE=15-16`.

Weight and height are **not** required here. Log them in the app, or let Google Health supply them. `WEIGHT_KG` / `HEIGHT_CM` in `.env` are only starting values.

## 3. Sign in to Google once

This opens your browser, so do it on a machine that has one (even if the service will later run on a headless box):

```sh
uv sync
uv run healthsync.py auth        # opens Google sign-in, writes credentials/token.json
uv run healthsync.py catchup     # optional: fetches the last 30 days into data/health.db
```

If Google warns the app is unverified, choose **Advanced → Go to (app name)**. That is expected for your own personal client.

On a headless server, copy `credentials/token.json` (and optionally `data/health.db`, which skips the backfill) to the same paths there. The refresh token lasts, as long as you published the app in step 1.6.

## 4. Choose a model

The assistant talks to models through [LiteLLM](https://docs.litellm.ai/docs/providers), so you pick a `provider/model` string and set that provider's key in `.env`. Choose one:

**Ollama Cloud** (the author's default)
```ini
ASSISTANT_MODEL=ollama_chat/glm-5.3-flash     # check ollama.com/search?c=cloud for current tags
OLLAMA_API_BASE=https://ollama.com
OLLAMA_API_KEY=<from ollama.com/settings/keys>
```

**A local Ollama** (free, private, nothing leaves your network)
```sh
ollama pull llama3.1
```
```ini
ASSISTANT_MODEL=ollama_chat/llama3.1
OLLAMA_API_BASE=http://host.docker.internal:11434
```
`host.docker.internal` works on Docker Desktop (Mac/Windows). On Linux add `extra_hosts: ["host.docker.internal:host-gateway"]` to the service in `docker-compose.yml`.

**OpenAI / Gemini / others**
```ini
ASSISTANT_MODEL=openai/gpt-5-mini        OPENAI_API_KEY=sk-...
# or
ASSISTANT_MODEL=gemini/gemini-2.5-flash  GEMINI_API_KEY=...
```

Optional knobs: `ASSISTANT_REASONING=low|medium|high` (reasoning models think before answering; `low` keeps replies quick) and `MEMORY_MODEL` (a smaller model just for rewriting `MEMORY.md`).

**Picking a model well:** the assistant relies on *tool calling* (it reads your data through tools rather than from memory). Use a model that supports it. If it states numbers without checking, or ignores the data, move to a larger model before changing any prompt.

> **Privacy:** your chat messages, `MEMORY.md` and the data the assistant looks up go to whichever provider you choose. A local Ollama keeps all of it on your machine.

## 5. Start it

```sh
docker compose up -d --build
docker compose logs -f
```

You should see `listening on :18787`, then the first catch-up fetching steps, sleep, exercise, heart rate. `restart: unless-stopped` brings it back after reboots and power cuts.

Open **http://localhost:18787**.

*No Docker?* Run it directly with systemd, see [`deploy/healthsync.service`](../deploy/healthsync.service) (you also need `chromium` for the PDF report).

## 6. First-time walkthrough

1. **Weight chip (top left):** set your height and log today's weight. If you weigh in through the Google Health app, those sync in by themselves.
2. **Chat:** the assistant opens the conversation with the most important thing it sees (a missed walk, a flat weight trend, a stale weigh-in). Answer it. Open **Memory** to see what it wrote down.
3. **Diet tab:** the first open builds your plan (about a minute). It aims for the healthy BMI range by default, or your goal weight if you set one in **Preferences**. Log what you eat in the box, or tell the assistant.
4. **Report tab:** your last 7 days as a web page. `/report.pdf` gives the PDF.
5. **Sync button:** forces a fetch from Google right now. **New chat** starts a clean conversation (memory is kept).

**Checklist that it is healthy**

- `curl localhost:18787/healthz` prints `ok`
- the Sync chip says *Synced just now*
- the Report tab shows charts, not an error
- the chat answers (if not, the red message names the problem; check the model settings)

## 7. Make it reachable from your phone (optional)

Use a tunnel instead of opening router ports. With a free [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/):

1. Add a public hostname, e.g. `health.example.com`, pointing at `http://localhost:18787`.
2. **Protect it.** The app has no login by default and anyone who reaches it can read your data and spend your model credit. Either:
   - put **Cloudflare Access** in front (recommended), with a *Bypass* policy for the path `/webhook` so Google can still reach it, or
   - set `REPORT_BASIC_AUTH=true` and `REPORT_PASSWORD=$(openssl rand -base64 24)` in `.env` (user is `REPORT_USER`, default `me`).
3. On your phone, open the URL and use **Add to Home Screen**.

## 8. Real-time updates with webhooks (optional)

Without webhooks it still syncs daily (plus `POLL_EVERY_HOURS` if set). With them, new data arrives within minutes of your watch syncing.

1. Needs the public hostname from step 7.
2. In `.env`: `GOOGLE_PROJECT_NUMBER`, `WEBHOOK_URL=https://health.example.com/webhook`, and `WEBHOOK_SECRET=$(openssl rand -hex 32)`. Then `docker compose up -d`.
3. Register the subscription once. This is the only step that needs the [`gcloud` CLI](https://cloud.google.com/sdk/docs/install), signed in as the project owner:
   ```sh
   gcloud auth login
   uv run healthsync.py subscribe
   ```
   Google calls your endpoint immediately to verify it: it must answer `201` with the secret and `401` without. Make sure the tunnel is up first.

## 9. Day-to-day

| Task | Command |
|---|---|
| Update | `git pull && docker compose up -d --build` |
| Logs | `docker compose logs -f` |
| Sync now | the **Sync** button, or `docker compose exec healthsync .venv/bin/python healthsync.py catchup` |
| Back up | copy `data/` (database + `MEMORY.md`) and `credentials/` |
| Reset the assistant's memory | delete `data/MEMORY.md` |
| Query your data | `sqlite3 data/health.db "SELECT * FROM weight_log"` |
| Run the tests | `uv run python -m unittest discover -s tests -v` |

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `redirect_uri_mismatch` at sign-in | Use a **Desktop** OAuth client. With a Web client, add `http://localhost:8080` exactly (no trailing slash) as a redirect URI. |
| Sync stops after ~7 days with `invalid_grant` | The app is still in *Testing*. Publish it (step 1.6), then run `uv run healthsync.py auth` again. |
| `ACCOUNT_NOT_LINKED` | Open the Google Health app on your phone and finish setting up your profile. |
| Sign-in page says the app isn't verified | Expected for a personal client: **Advanced → Go to (app)**. |
| Chat shows "The assistant is unavailable" | Check `ASSISTANT_MODEL` and that provider's key in `.env`, then `docker compose up -d`. `docker compose logs` has the cause. |
| Plan or chat is very slow | You are using a reasoning model. Set `ASSISTANT_REASONING=low`, or use a faster model. Plan generation takes up to about a minute. |
| Assistant gives vague or unchecked answers | The model is too small for tool calling. Use a larger one. |
| Report tab says "Report unavailable" | Needs at least one day of steps and one night of sleep, plus a weight and height. Run **Sync**, then log your weight. |
| `subscribe` fails with `FAILED_PRECONDITION` | Google could not verify the endpoint. Check the tunnel, `WEBHOOK_SECRET`, and that `https://<host>/healthz` answers. |
| Chromium crashes (`SIGABRT`) on the host | Set `CHROME_NO_SANDBOX=1` in `.env`. |
| Diet tab says nothing yet | It needs a weight and height first (the weight chip), and a model that works. |

Still stuck? `docker compose logs --tail 100` and the red message in the app are the two places to look.
