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
"""


def db_path() -> str:
    return os.getenv("DATABASE_PATH", "./li_comments.db")


async def _migrate(db: aiosqlite.Connection) -> None:
    cur = await db.execute("PRAGMA table_info(handles)")
    cols = [r[1] for r in await cur.fetchall()]
    if "deleted_at" not in cols:
        await db.execute("ALTER TABLE handles ADD COLUMN deleted_at TEXT")


async def init_db() -> None:
    async with aiosqlite.connect(db_path()) as db:
        await db.executescript(SCHEMA)
        await _migrate(db)
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
