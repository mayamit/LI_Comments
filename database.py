import os
from contextlib import asynccontextmanager
from typing import Optional

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
    handle_id INTEGER REFERENCES handles(id),   -- NULL for trending posts until the author is promoted
    post_id TEXT UNIQUE NOT NULL,
    content TEXT,
    summary TEXT,
    url TEXT,
    engagement_json TEXT,
    posted_at TEXT,
    fetched_at TEXT DEFAULT (datetime('now')),
    status TEXT DEFAULT 'unreviewed',
    source TEXT DEFAULT 'monitored',            -- 'monitored' | 'trending'
    engagement_score INTEGER,                    -- cached rank key for trending
    author_handle TEXT,                          -- trending: inline author (no handles row yet)
    author_name TEXT
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
    dimension TEXT NOT NULL CHECK(dimension IN ('persona','reach','intent','cadence','roster')),
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

CREATE TABLE IF NOT EXISTS discovery_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT UNIQUE NOT NULL,
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS discovery_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    queries TEXT,            -- JSON array of search queries used
    window TEXT,             -- postedLimit value
    started_at TEXT NOT NULL,
    ended_at TEXT,
    posts_found INTEGER DEFAULT 0,
    posts_kept INTEGER DEFAULT 0,
    skipped_duplicates INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    summary_json TEXT
);
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
    # Roster — which handles you engage with on a given day. Applying a roster
    # activates exactly its members, so these drive who the fetch picks up.
    ("roster-monday", "Monday", "roster", "Engage with these on Mondays", 10),
    ("roster-tuesday", "Tuesday", "roster", "Engage with these on Tuesdays", 20),
    ("roster-wednesday", "Wednesday", "roster", "Engage with these on Wednesdays", 30),
    ("roster-thursday", "Thursday", "roster", "Engage with these on Thursdays", 40),
    ("roster-friday", "Friday", "roster", "Engage with these on Fridays", 50),
    ("roster-saturday", "Saturday", "roster", "Engage with these on Saturdays", 60),
    ("roster-sunday", "Sunday", "roster", "Engage with these on Sundays", 70),
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
    if "source" not in cols:
        await db.execute("ALTER TABLE posts ADD COLUMN source TEXT DEFAULT 'monitored'")
    if "engagement_score" not in cols:
        await db.execute("ALTER TABLE posts ADD COLUMN engagement_score INTEGER")
    if "author_handle" not in cols:
        await db.execute("ALTER TABLE posts ADD COLUMN author_handle TEXT")
    if "author_name" not in cols:
        await db.execute("ALTER TABLE posts ADD COLUMN author_name TEXT")
    # Index created here (not in SCHEMA) so it runs after the source column exists.
    await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_source ON posts(source)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_fetched_at ON posts(fetched_at)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_generated_comments_post ON generated_comments(post_id)"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_posted_log_post ON posted_log(post_id)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_posted_log_comment ON posted_log(comment_id)"
    )

    cur = await db.execute("PRAGMA table_info(posted_log)")
    cols = [r[1] for r in await cur.fetchall()]
    if "rating" not in cols:
        await db.execute("ALTER TABLE posted_log ADD COLUMN rating INTEGER")
    if "rated_at" not in cols:
        await db.execute("ALTER TABLE posted_log ADD COLUMN rated_at TEXT")

    await _migrate_tag_dimensions(db)


async def _migrate_tag_dimensions(db: aiosqlite.Connection) -> None:
    """Widen the tags.dimension CHECK constraint when a dimension is added.

    SQLite cannot alter a CHECK in place, so the table is rebuilt. Row ids are
    carried over, which keeps handle_tags rows pointing at the right tags. The
    rebuild runs with foreign keys off so dropping the old table does not
    cascade-delete those rows.
    """
    cur = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tags'"
    )
    row = await cur.fetchone()
    if not row or "'roster'" in row[0]:
        return

    # PRAGMA foreign_keys is a no-op inside a transaction, so settle first.
    await db.commit()
    await db.execute("PRAGMA foreign_keys = OFF")
    try:
        await db.execute(
            """
            CREATE TABLE tags_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE NOT NULL,
                label TEXT NOT NULL,
                dimension TEXT NOT NULL CHECK(dimension IN
                    ('persona','reach','intent','cadence','roster')),
                description TEXT,
                sort_order INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                deleted_at TEXT
            )
            """
        )
        await db.execute(
            "INSERT INTO tags_new "
            "(id, slug, label, dimension, description, sort_order, created_at, deleted_at) "
            "SELECT id, slug, label, dimension, description, sort_order, created_at, deleted_at "
            "FROM tags"
        )
        await db.execute("DROP TABLE tags")
        await db.execute("ALTER TABLE tags_new RENAME TO tags")
        await db.commit()
    finally:
        await db.execute("PRAGMA foreign_keys = ON")


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


async def _seed_discovery_topics(db: aiosqlite.Connection) -> None:
    """Seed trending-discovery topics from DISCOVERY_QUERIES, but only when the
    table is empty. Topics are editable afterwards (DB now, UI later) without a
    code change, so this never overwrites the user's curated list."""
    cur = await db.execute("SELECT 1 FROM discovery_topics LIMIT 1")
    if await cur.fetchone() is not None:
        return
    raw = os.getenv("DISCOVERY_QUERIES", "")
    queries = [q.strip() for q in raw.split(",") if q.strip()]
    if queries:
        await db.executemany(
            "INSERT OR IGNORE INTO discovery_topics (query) VALUES (?)",
            [(q,) for q in queries],
        )


async def init_db() -> None:
    async with aiosqlite.connect(db_path()) as db:
        # Only takes effect on a brand-new file; an existing database switches
        # over via `python maintenance.py --vacuum`.
        await db.execute("PRAGMA auto_vacuum = INCREMENTAL")
        await db.executescript(SCHEMA)
        await _migrate(db)
        await _seed_tags(db)
        await _seed_handle(db)
        await _seed_discovery_topics(db)
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


async def unmark_posted(log_id: int, max_age_s: Optional[int] = None) -> Optional[str]:
    """Remove a posted_log entry and return its post to 'reviewed'.

    With max_age_s, entries older than that are refused (the undo window).
    Returns an error message, or None on success.
    """
    async with get_db() as db:
        cur = await db.execute(
            "SELECT post_id, posted_at >= datetime('now', ?) AS fresh "
            "FROM posted_log WHERE id = ?",
            (f"-{max_age_s or 0} seconds", log_id),
        )
        row = await cur.fetchone()
        if not row:
            return "Already undone or removed."
        if max_age_s is not None and not row["fresh"]:
            return f"Undo window expired ({max_age_s // 60} minutes)."
        await db.execute("DELETE FROM posted_log WHERE id = ?", (log_id,))
        await db.execute(
            "UPDATE posts SET status = 'reviewed' WHERE id = ?", (row["post_id"],)
        )
        await db.commit()
    return None
