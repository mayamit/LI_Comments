import os
from contextlib import asynccontextmanager

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS handles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    linkedin_handle TEXT UNIQUE NOT NULL,
    display_name TEXT,
    active INTEGER DEFAULT 1,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    last_fetched_at TEXT,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handle_id INTEGER REFERENCES handles(id),
    post_id TEXT UNIQUE NOT NULL,
    content TEXT,
    summary TEXT,
    url TEXT,
    engagement_json TEXT,
    posted_at TEXT,
    fetched_at TEXT DEFAULT (datetime('now')),
    status TEXT DEFAULT 'unreviewed'
);

CREATE TABLE IF NOT EXISTS generated_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER REFERENCES posts(id),
    tone TEXT NOT NULL,
    content TEXT NOT NULL,
    edited INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS posted_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER REFERENCES posts(id),
    comment_id INTEGER REFERENCES generated_comments(id),
    tone TEXT,
    posted_at TEXT DEFAULT (datetime('now')),
    notes TEXT,
    rating INTEGER,
    rated_at TEXT
);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    handles_processed INTEGER DEFAULT 0,
    new_posts INTEGER DEFAULT 0,
    skipped_duplicates INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    summary_json TEXT
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    dimension TEXT NOT NULL CHECK(dimension IN ('persona','reach','intent','cadence')),
    description TEXT,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS handle_tags (
    handle_id INTEGER NOT NULL REFERENCES handles(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (handle_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_handle_tags_tag ON handle_tags(tag_id);
"""

# (slug, label, dimension, description, sort_order)
SEED_TAGS = [
    # Persona — what hat they wear in their posts
    ("founder", "Founder", "persona", "Posts from a founder lens", 10),
    ("ceo", "CEO", "persona", "C-suite leadership perspective", 20),
    ("exec", "Exec / VP", "persona", "Senior leader, not founder", 30),
    ("operator", "Operator", "persona", "Director/manager-level, in-the-weeds", 40),
    ("product", "Product", "persona", "Product management voice", 50),
    ("engineering", "Engineering", "persona", "Eng leader or builder", 60),
    ("design", "Design", "persona", "Design leader or practitioner", 70),
    ("marketing", "Marketing", "persona", "Marketing / growth leader", 80),
    ("sales", "Sales", "persona", "Sales leader or rep", 90),
    ("recruiter", "Recruiter", "persona", "External or in-house recruiter", 100),
    ("investor-vc", "Investor — VC", "persona", "Venture capital", 110),
    ("investor-pe", "Investor — PE", "persona", "Private equity", 120),
    ("creator", "Creator", "persona", "LinkedIn content as their main thing", 130),
    ("coach", "Coach", "persona", "Executive or career coach", 140),
    ("analyst", "Analyst", "persona", "Industry / research analyst", 150),
    # Reach — audience size
    ("reach-mega", "Mega (100k+)", "reach", "Mega audience, 100k+ followers", 10),
    ("reach-large", "Large (10–100k)", "reach", "Large audience, 10k–100k followers", 20),
    ("reach-mid", "Mid (1–10k)", "reach", "Mid audience, 1k–10k followers", 30),
    ("reach-niche", "Niche (<1k)", "reach", "Small but often high-conversion audience", 40),
    # Intent — why they're on your list
    ("prospect", "Prospect", "intent", "Potential customer / buyer", 10),
    ("network", "Network", "intent", "Peer relationship", 20),
    ("hiring-signal", "Hiring signal", "intent", "Recruiters or hiring managers", 30),
    ("thought-leader", "Thought leader", "intent", "You learn from them", 40),
    ("industry-watch", "Industry watch", "intent", "Vertical pulse / trend signal", 50),
    # Cadence — posting frequency
    ("cadence-daily", "Daily", "cadence", "Posts daily", 10),
    ("cadence-weekly", "Weekly", "cadence", "Posts roughly weekly", 20),
    ("cadence-sporadic", "Sporadic", "cadence", "Posts occasionally", 30),
]


# Seeded into a brand-new (empty) handles table so a fresh install isn't blank.
# (linkedin_handle, display_name, active, notes)
SEED_HANDLE = (
    "agandhi5",
    "Amit Gandhi - Co-Founder and CTO at Parkar, AI Visionary",
    1,
    "Owner — seeded on first run",
)


def db_path() -> str:
    return os.getenv("DATABASE_PATH", "./li_comments.db")


async def _migrate(db: aiosqlite.Connection) -> None:
    cur = await db.execute("PRAGMA table_info(handles)")
    cols = [r[1] for r in await cur.fetchall()]
    if "deleted_at" not in cols:
        await db.execute("ALTER TABLE handles ADD COLUMN deleted_at TEXT")
    if "enrichment_json" not in cols:
        await db.execute("ALTER TABLE handles ADD COLUMN enrichment_json TEXT")
    if "enriched_at" not in cols:
        await db.execute("ALTER TABLE handles ADD COLUMN enriched_at TEXT")

    cur = await db.execute("PRAGMA table_info(posts)")
    cols = [r[1] for r in await cur.fetchall()]
    if "summary" not in cols:
        await db.execute("ALTER TABLE posts ADD COLUMN summary TEXT")

    cur = await db.execute("PRAGMA table_info(posted_log)")
    cols = [r[1] for r in await cur.fetchall()]
    if "rating" not in cols:
        await db.execute("ALTER TABLE posted_log ADD COLUMN rating INTEGER")
    if "rated_at" not in cols:
        await db.execute("ALTER TABLE posted_log ADD COLUMN rated_at TEXT")


async def _seed_tags(db: aiosqlite.Connection) -> None:
    await db.executemany(
        """
        INSERT OR IGNORE INTO tags (slug, label, dimension, description, sort_order)
        VALUES (?, ?, ?, ?, ?)
        """,
        SEED_TAGS,
    )


async def _seed_handle(db: aiosqlite.Connection) -> None:
    """Seed the owner handle, but only when the handles table is completely
    empty. This gives a fresh install a starting entry without ever re-adding
    it if the user later deletes it or on an already-populated database."""
    cur = await db.execute("SELECT 1 FROM handles LIMIT 1")
    if await cur.fetchone() is not None:
        return
    await db.execute(
        """
        INSERT INTO handles (linkedin_handle, display_name, active, notes)
        VALUES (?, ?, ?, ?)
        """,
        SEED_HANDLE,
    )


async def init_db() -> None:
    async with aiosqlite.connect(db_path()) as db:
        await db.executescript(SCHEMA)
        await _migrate(db)
        await _seed_tags(db)
        await _seed_handle(db)
        await db.commit()


@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(db_path())
    db.row_factory = aiosqlite.Row
    try:
        await db.execute("PRAGMA foreign_keys = ON")
        yield db
    finally:
        await db.close()
