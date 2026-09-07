# LI_Comments — Claude Code Guide

## Project Overview
A personal LinkedIn engagement tool. It monitors a curated list of LinkedIn profiles, fetches their latest posts daily, generates one comment option per active tone per post using Claude (each with a distinct tone), and presents them in a morning dashboard for review. Posting to LinkedIn is always done manually by the user — this app never writes to LinkedIn.

## Tech Stack
| Layer | Choice |
|---|---|
| Backend | FastAPI (Python) |
| Frontend | Jinja2 templates + HTMX (no React) |
| Database | SQLite via raw `sqlite3` or `aiosqlite` |
| Scheduler | APScheduler (runs inside the app) |
| LLM | Claude via the `claude` CLI (Claude Code subscription); model `claude-sonnet-4-6` |
| LinkedIn data | Apify (`harvestapi~linkedin-profile-posts`) |

## Project Structure
```
LI_Comments/
├── main.py                  # FastAPI app entry point, scheduler init
├── database.py              # DB connection, schema init, query helpers
├── agent.py                 # Fetch + generate pipeline (Apify → Claude)
├── discover.py              # Trending post discovery (Apify post-search → rank → Claude)
├── backfill.py              # Regenerate comments for posts that have none (CLI-logout recovery)
├── tones.yaml               # All tone prompt templates (single source of truth)
├── tones.py                 # Loads/saves tones.yaml; tone helpers
├── routers/
│   ├── admin.py             # Handle CRUD, run-now trigger
│   ├── dashboard.py         # Morning feed, comment selection
│   └── history.py           # Posted log, stats
├── templates/
│   ├── base.html
│   ├── admin.html
│   ├── dashboard.html
│   └── history.html
├── static/                  # CSS, minimal JS
├── .env                     # Never committed
├── .env.example
└── requirements.txt
```

## Database Schema
```sql
CREATE TABLE handles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    linkedin_handle TEXT UNIQUE NOT NULL,
    display_name TEXT,
    active INTEGER DEFAULT 1,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    last_fetched_at TEXT
);

CREATE TABLE posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handle_id INTEGER REFERENCES handles(id),
    post_id TEXT UNIQUE NOT NULL,   -- LinkedIn post ID, dedup key
    content TEXT,
    url TEXT,
    engagement_json TEXT,           -- raw JSON blob
    posted_at TEXT,
    fetched_at TEXT DEFAULT (datetime('now')),
    status TEXT DEFAULT 'unreviewed' -- unreviewed | reviewed | posted | dismissed
);

CREATE TABLE generated_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER REFERENCES posts(id),
    tone TEXT NOT NULL,
    content TEXT NOT NULL,
    edited INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE posted_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER REFERENCES posts(id),
    comment_id INTEGER REFERENCES generated_comments(id),
    tone TEXT,
    posted_at TEXT DEFAULT (datetime('now')),
    notes TEXT
);
```

## The Tones
Defined in `tones.yaml` (loaded via `tones.py`) — the single source of truth; the
count is not fixed. Never hardcode tone logic elsewhere. Adding a tone = adding one
entry to `tones.yaml`. A tone is active unless it has `active: false`
(`tones.is_active` defaults a missing `active` field to `True`); only active tones
generate comments.

| Key | Name | Intent | Active |
|---|---|---|---|
| `operator` | Operator Lens | Ground-level, practical execution perspective | yes |
| `strategic` | Strategic | Big picture, market or business angle | yes |
| `curious` | Curious | Question-led, invites dialogue | yes |
| `contrarian` | Contrarian | Respectful pushback or alternative view | yes |
| `affirming` | Affirming | Builds on their point, adds a layer | **no** |
| `concise` | Concise | One punchy sentence | yes |
| `reference` | Popular Reference | Cites a book, song, or pop-culture reference | yes |
| `secondorder` | Second Order | Forward-looking; the second-order effect most people miss | yes |
| `wry` | Wry | Executive observational humor — one dry line | yes |
| `analogy` | Analogy | One original metaphor from an everyday domain | yes |
| `personal` | Personal | Warm/familiar, for people you actually know | yes |

## Handle Tags & Rosters
Handles are grouped many-to-many via `tags` + `handle_tags`. Every tag belongs to
one **dimension**, enforced by a `CHECK` constraint on `tags.dimension`:

| Dimension | Describes | Examples |
|---|---|---|
| `persona` | What hat they wear in their posts | Founder, CEO, Operator |
| `reach` | Audience size | Mega (100k+), Niche (<1k) |
| `intent` | Why they're on your list | Prospect, Network, Thought leader |
| `cadence` | How often *they* post | Daily, Weekly, Sporadic |
| `roster` | Which day *you* engage with them | Monday … Sunday |

The first four are descriptive attributes. `roster` is operational: it names a
saved working set. **Apply roster** (`POST /admin/handles/apply-roster`) sets
`active = 1` for exactly that roster's handles and `active = 0` for all others,
so the next fetch — which selects `WHERE active = 1` — picks up precisely that
day's list. Applying an empty roster is refused, since it would deactivate every
handle and leave the fetch with nothing to do.

Adding a dimension means widening the `CHECK` constraint, and SQLite cannot
alter one in place: `database._migrate_tag_dimensions` rebuilds the `tags` table,
preserving row ids so `handle_tags` stays valid. The dimension must also be added
to `DIMENSIONS` / `DIMENSION_LABELS` in **both** `routers/admin.py` and
`routers/tag_admin.py`.

## Environment Variables
Comment generation runs through the `claude` CLI (Claude Code subscription),
**not** the Anthropic API — there is no `ANTHROPIC_API_KEY`. See `.env.example`.
```
APIFY_TOKEN=                 # required — Apify access for LinkedIn post fetching
OWNER_NAME=Amit Gandhi       # name shown in the app header (default: Amit Gandhi)
CLAUDE_CLI=claude            # optional — path to the claude CLI (default: on PATH)
CLAUDE_MODEL=claude-sonnet-4-6
CLAUDE_TIMEOUT_S=120
FETCH_SCHEDULE_ENABLED=0     # set 1 to enable the daily scheduled fetch
FETCH_SCHEDULE_HOUR=6        # 24h, default 6am
FETCH_SCHEDULE_MINUTE=0
DISCOVERY_QUERIES=           # comma-separated topics; seeds discovery_topics on first run
DISCOVERY_WINDOW=week        # any|1h|24h|week|month|3months|6months|year
DISCOVERY_MAX_POSTS_PER_QUERY=25
DISCOVERY_KEEP_TOP_N=10      # top-N posts kept per discovery run, ranked by engagement
DISCOVERY_SCHEDULE_ENABLED=0 # set 1 to enable a scheduled daily discovery run
DISCOVERY_SCHEDULE_HOUR=7    # 24h, default 7am
DISCOVERY_SCHEDULE_MINUTE=0
DATABASE_PATH=./li_comments.db
LOG_LEVEL=INFO
LOG_DIR=./logs
```

## Key Conventions
- **No LinkedIn writes** — this app never posts, comments, or interacts with LinkedIn programmatically
- **Deduplication** — always check `posts.post_id` before inserting; skip silently if exists
- **Tone config** — all prompt templates live in `tones.yaml`; adding a tone = adding one entry there, nothing else
- **Tag dimensions** — adding one means widening the `tags.dimension` CHECK (via a table rebuild) *and* updating `DIMENSIONS`/`DIMENSION_LABELS` in both routers
- **HTMX over JavaScript** — prefer HTMX attributes for dynamic UI (inline edit, copy button, status filters) over writing custom JS
- **No ORMs** — use raw SQL via `sqlite3`/`aiosqlite` to keep the dependency footprint minimal
- **Errors don't abort runs** — if Apify or Claude fails for one handle/tone, log it and continue; never let one failure kill the whole agent run

## External Services
### Apify
- Actor: `harvestapi~linkedin-profile-posts`
- Input: `{ "targetUrls": ["https://www.linkedin.com/in/{handle}"], "maxPosts": 1 }`
- Poll `GET /v2/actor-runs/{run_id}` until status is `SUCCEEDED` or `FAILED`
- Fetch results from `GET /v2/datasets/{dataset_id}/items`
- Post content is in the `content` field (Unicode NFKD-normalize it to strip LinkedIn bold/italic)

### Claude (via the `claude` CLI)
- Comment generation shells out to the `claude` CLI (`comments.py`), using the
  user's Claude Code subscription — **no Anthropic API key, no per-call cost**.
- Invocation: `claude -p <prompt> --model <CLAUDE_MODEL>`, run from a temp cwd so
  the CLI doesn't load this project's `CLAUDE.md` as context.
- Model: `claude-sonnet-4-6` (override via `CLAUDE_MODEL`); CLI path via `CLAUDE_CLI`.
- One CLI call per tone per post, run in parallel; timeout `CLAUDE_TIMEOUT_S` (120s).
- Keep comments to 1–4 sentences, LinkedIn reply length.
- **Prerequisite:** the `claude` CLI must be installed and on `PATH` (or `CLAUDE_CLI`
  set), and logged in. Without it, comment generation fails for every tone.

## GitHub Issues
- Epics: #1–#6
- User stories: #7–#29
- Label conventions: `epic`, `user-story`, plus one feature label per epic (e.g. `dashboard`, `fetching-agent`)

## Running Locally
```bash
cp .env.example .env       # fill in keys
pip install -r requirements.txt
uvicorn main:app --reload  # DB auto-initialises on first run
```
App runs at `http://localhost:8000`
