# LI_Comments

A personal LinkedIn engagement tool. Monitors a curated list of profiles, fetches their latest posts daily, generates 6 tonally-distinct comment options per post with Claude, and presents them in a morning review dashboard. Posting to LinkedIn is always manual — this app never writes to LinkedIn.

See [`CLAUDE.md`](./CLAUDE.md) for architecture, schema, and conventions.

## Quick start

```bash
cp .env.example .env       # then fill in ANTHROPIC_API_KEY and APIFY_TOKEN
make install               # pip install -r requirements.txt
make dev                   # uvicorn main:app --reload
```

App runs at `http://localhost:8000`. The SQLite database auto-initialises on first start. Health check: `GET /health`.

## Required environment variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API access (comment generation) |
| `APIFY_TOKEN` | Apify access (LinkedIn post fetching) |

Optional: `FETCH_SCHEDULE_HOUR`, `FETCH_SCHEDULE_MINUTE`, `DATABASE_PATH` — see `.env.example`.
