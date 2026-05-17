import os
import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS handles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    linkedin_handle TEXT UNIQUE NOT NULL,
    display_name TEXT,
    active INTEGER DEFAULT 1,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    last_fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handle_id INTEGER REFERENCES handles(id),
    post_id TEXT UNIQUE NOT NULL,
    content TEXT,
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
    notes TEXT
);
"""


def db_path() -> str:
    return os.getenv("DATABASE_PATH", "./li_comments.db")


async def init_db() -> None:
    async with aiosqlite.connect(db_path()) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def connect() -> aiosqlite.Connection:
    db = await aiosqlite.connect(db_path())
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    return db
