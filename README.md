# LI_Comments

A personal LinkedIn engagement tool. Monitors a curated list of profiles, fetches their latest posts daily, generates 6 tonally-distinct comment options per post with Claude, and presents them in a morning review dashboard. Posting to LinkedIn is always manual — this app never writes to LinkedIn.

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

## Required environment variables

| Variable | Purpose |
|---|---|
| `APIFY_TOKEN` | Apify access (LinkedIn post fetching) |

Comment generation uses the `claude` CLI (Claude Code subscription) rather than the Anthropic API, so no API key is required. The CLI must be installed and on `PATH`. Override with `CLAUDE_CLI` and `CLAUDE_MODEL` if needed.

Optional: `FETCH_SCHEDULE_HOUR`, `FETCH_SCHEDULE_MINUTE`, `DATABASE_PATH`, `CLAUDE_TIMEOUT_S` — see `.env.example`.
