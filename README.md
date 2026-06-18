# LI_Comments

A personal LinkedIn engagement tool. Monitors a curated list of profiles, fetches their latest posts daily, generates seven tonally-distinct comment options per post with Claude, and presents them in a morning review dashboard. Posting to LinkedIn is always manual — this app never writes to LinkedIn.

See [`CLAUDE.md`](./CLAUDE.md) for architecture, schema, and conventions.

## Prerequisites

- **Python 3.10+** (3.12 recommended).
- **An Apify account + API token** — for fetching LinkedIn posts. Set as `APIFY_TOKEN`.
- **The `claude` CLI installed, on `PATH`, and logged in** — comment generation runs
  through the [Claude Code](https://claude.com/claude-code) CLI (your Pro/Max
  subscription), *not* the Anthropic API. Without it, no comments are generated.
  Verify with `claude --version`.

## Quick start (macOS / Linux)

```bash
python3 -m venv .venv && source .venv/bin/activate
cp .env.example .env             # then fill in APIFY_TOKEN
cp tones.example.yaml tones.yaml # starter voice config (personalise it below)
make install                     # pip install -r requirements.txt
make dev                         # uvicorn main:app --reload
```

App runs at `http://localhost:8000`. The SQLite database auto-initialises on first start. Health check: `GET /health`.

To run the server detached (logs to `./logs/server.out`), use `python start.py`
(`--force` to restart an already-running instance).

## Quick start (Windows)

`make`, `start.sh`, and `start.py` rely on Unix tooling for some paths; on Windows
run the steps directly in PowerShell or Command Prompt:

```powershell
python -m venv .venv
.venv\Scripts\activate
copy .env.example .env             # then edit and fill in APIFY_TOKEN
copy tones.example.yaml tones.yaml # starter voice config
pip install -r requirements.txt
python -m uvicorn main:app --reload
```

`python start.py` also works on Windows (it uses `psutil` for port detection, not
`lsof`); run it with `python start.py`, not `./start.py`.

## Personalise it to your own voice

The app generates comments from `tones.yaml`. `tones.example.yaml` is a working
default, but to make it sound like *you*, fill in a voice profile and convert it:

```bash
cp voice-profile-template.md my-voice-profile.md   # then fill it in
python voice_to_tones.py my-voice-profile.md --force
```

The converter distils your profile into the shared system prompt and a one-line
example reply per tone, while keeping the seven-tone structure fixed. It runs
through the same `claude` CLI used for comments, so no API key is needed. Review
the generated `tones.yaml`, then start the app.

Your `tones.yaml` and your personal profile stay local (gitignored) — only the
template and example are tracked, so each user keeps their own voice.

## Using the app

The app never posts to LinkedIn — every comment is copied and posted by you manually. Day to day it works in three loops.

### 1. Monitor → review (the core loop)

1. **Add profiles** on the **Handles** page (the `linkedin.com/in/<handle>` slug). Mark the ones you want fetched as active.
2. **Fetch posts** — click **Run Now** on the Handles page (or enable the daily schedule, below). For each new post the app stores it, writes a TL;DR summary, and generates the seven tonally-distinct comment options.
3. **Review** on the **Dashboard**. Each post shows the summary, the original, and the seven options. Edit any option inline, **Copy** it (which marks the post reviewed), then paste it into LinkedIn yourself. **Mark posted** records which tone you used; **Dismiss** drops posts you're skipping. Tabs filter by status (Unreviewed / Reviewed / Posted / Dismissed).

### 2. Auto-tag (profile enrichment)

On the **Handles** page, **Auto-tag** enriches untagged handles via Apify: it derives reach (follower buckets), posting cadence, and 1–2 persona tags, and sets a clean display name. Intent tags are left alone — those are your manual call. (First run may need a one-time Apify actor approval — see below.)

### 3. Trending discovery

The **Trending** page finds the highest-engagement posts on topics you care about, beyond your monitored list.

1. **Add topics** in the Topics bar (e.g. `AI agents`, `platform engineering`). Toggle a topic to enable/disable it, or remove it. Topics persist in the database — no `.env` edit needed. `DISCOVERY_QUERIES` only seeds the initial list on a fresh install.
2. **Run discovery** — pick a time window (`24h … year`) and how many top posts to keep, then **Run discovery now**. The app searches via Apify, ranks results by engagement (`reactions + 2·comments + 3·reposts`), keeps the top N, and runs them through the same summary + seven-tone pipeline.
3. **Review** the ranked posts (🔥 = engagement score) just like the dashboard. **Monitor this author** promotes a discovered author into your monitored Handles list so their future posts are fetched automatically.

### Scheduling (optional)

Both the daily fetch and daily discovery are off by default. Enable them in `.env`:

- `FETCH_SCHEDULE_ENABLED=1` (+ `FETCH_SCHEDULE_HOUR`/`MINUTE`) — daily monitored-handle fetch.
- `DISCOVERY_SCHEDULE_ENABLED=1` (+ `DISCOVERY_SCHEDULE_HOUR`/`MINUTE`) — daily trending discovery.

Restart the server after changing `.env`. Without these, use the **Run Now** buttons on demand.

## Required environment variables

| Variable | Purpose |
|---|---|
| `APIFY_TOKEN` | Apify access (LinkedIn post fetching) |
| `OWNER_NAME` | Name shown in the app header (default: `Amit Gandhi`) |

Comment generation uses the `claude` CLI (Claude Code subscription) rather than the Anthropic API, so no API key is required. The CLI must be installed and on `PATH`. Override with `CLAUDE_CLI` and `CLAUDE_MODEL` if needed.

Optional: `FETCH_SCHEDULE_HOUR`, `FETCH_SCHEDULE_MINUTE`, `DATABASE_PATH`, `CLAUDE_TIMEOUT_S`, and the `DISCOVERY_*` trending-discovery settings — see `.env.example`.

## Apify: approve the actor on first run

The first time you use **Handles → Auto-tag** (profile enrichment) or **Trending**, Apify may reject the run with `403 full-permission-actor-not-approved`. The `harvestapi` actors request account access that you approve once: open the actor in the [Apify console](https://console.apify.com/) (e.g. `harvestapi/linkedin-profile-scraper`), approve its permissions, then re-run. Pay-per-result actors draw from your Apify credit (the free plan includes a monthly allowance), so no top-up is needed for normal personal use.

## Running the tests

```bash
pip install -r requirements.txt   # includes pytest + pytest-asyncio
pytest
```

Tests use a temporary SQLite database per test and never call Apify or the `claude` CLI, so they run offline.
