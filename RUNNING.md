# Running the Server

## Start

```bash
.venv/bin/uvicorn main:app --reload
```

App runs at http://localhost:8000 (redirects to the dashboard).

Run in the background by appending `&`, or use a separate terminal so reload logs stay visible.

## Stop

If running in the foreground: `Ctrl+C`.

Otherwise, kill whatever is bound to port 8000:

```bash
lsof -ti :8000 | xargs kill
```

Force-kill if it doesn't exit:

```bash
lsof -ti :8000 | xargs kill -9
```

## Notes

- Always use `.venv/bin/uvicorn`, not bare `uvicorn` — the system Python is missing `apscheduler` and other deps.
- First-time setup: `cp .env.example .env` (fill in keys), then `.venv/bin/pip install -r requirements.txt`.
- The DB auto-initialises on first run.
- Daily fetch is scheduled at 06:00 local (configurable via `FETCH_SCHEDULE_HOUR` / `FETCH_SCHEDULE_MINUTE` in `.env`).
