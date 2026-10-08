"""Retention: archive and slim down posts older than RETENTION_DAYS.

For posts fetched more than RETENTION_DAYS ago (default 15):
  - unreviewed posts are auto-dismissed (too old to be worth a comment);
  - the full post rows are copied to the archive database first;
  - generated comments you didn't post are archived, then deleted;
  - engagement_json is cut down to the counts and image URLs the dashboard uses.
The post rows themselves stay, because the fetch dedups on posts.post_id and
/history joins posted_log to posts. Leftover enrichment_json profile blobs on
handles are archived and cleared as well.

    python maintenance.py              # prune + incremental vacuum
    python maintenance.py --dry-run    # report what would change
    python maintenance.py --vacuum     # prune, then a full VACUUM (one-time:
                                       # switches the file to incremental
                                       # auto-vacuum). Stop the app first.
"""
import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys

from dotenv import load_dotenv

load_dotenv()

from database import db_path, get_db, init_db  # noqa: E402
from logging_setup import setup_logging  # noqa: E402
from utils import extract_post_images  # noqa: E402

logger = logging.getLogger("maintenance")

_running = False


def retention_days() -> int:
    return int(os.getenv("RETENTION_DAYS", "15"))


def cutoff_modifier() -> str:
    """SQLite datetime() modifier for the retention cutoff, e.g. '-15 days'."""
    return f"-{retention_days()} days"


def archive_path() -> str:
    explicit = os.getenv("ARCHIVE_DATABASE_PATH")
    if explicit:
        return explicit
    root, ext = os.path.splitext(db_path())
    return f"{root}_archive{ext or '.db'}"


def slim_engagement(raw: str) -> str:
    """Drop the stored Apify item, keeping counts and image URLs."""
    data = json.loads(raw)
    images = data.get("images")
    if images is None:
        images = extract_post_images(data.get("raw") or {})
    return json.dumps(
        {
            "reactions": data.get("reactions"),
            "comments": data.get("comments"),
            "reposts": data.get("reposts"),
            "images": images,
        }
    )


_OLD_POST = "p.fetched_at < datetime('now', ?)"
_UNPOSTED_COMMENT = (
    "NOT EXISTS (SELECT 1 FROM posted_log pl WHERE pl.comment_id = g.id)"
)


async def _ensure_archive_table(db, table: str) -> list[str]:
    """Create/extend archive.<table> to mirror main.<table>; return its columns.

    Columns are mirrored rather than copied once, so a later ALTER TABLE on the
    main schema doesn't break the INSERT ... SELECT.
    """
    cur = await db.execute(f"PRAGMA main.table_info({table})")
    cols = [(r[1], r[2]) for r in await cur.fetchall()]
    defs = ", ".join(f"{n} {t}" for n, t in cols if n != "id")
    await db.execute(
        f"CREATE TABLE IF NOT EXISTS archive.{table} (id INTEGER PRIMARY KEY, {defs}, "
        "archived_at TEXT DEFAULT (datetime('now')))"
    )
    cur = await db.execute(f"PRAGMA archive.table_info({table})")
    have = {r[1] for r in await cur.fetchall()}
    for n, t in cols:
        if n not in have:
            await db.execute(f"ALTER TABLE archive.{table} ADD COLUMN {n} {t}")
    return [n for n, _ in cols]


async def _counts(db, cutoff: str) -> dict:
    async def one(sql: str, params: tuple = ()) -> int:
        cur = await db.execute(sql, params)
        return (await cur.fetchone())[0]

    return {
        "dismissed": await one(
            f"SELECT COUNT(*) FROM posts p WHERE p.status = 'unreviewed' AND {_OLD_POST}",
            (cutoff,),
        ),
        "comments_dropped": await one(
            "SELECT COUNT(*) FROM generated_comments g JOIN posts p ON g.post_id = p.id "
            f"WHERE {_OLD_POST} AND {_UNPOSTED_COMMENT}",
            (cutoff,),
        ),
        "posts_slimmed": await one(
            f"SELECT COUNT(*) FROM posts p WHERE {_OLD_POST} "
            "AND p.engagement_json LIKE '%\"raw\"%'",
            (cutoff,),
        ),
        "profiles_cleared": await one(
            "SELECT COUNT(*) FROM handles WHERE enrichment_json IS NOT NULL"
        ),
    }


async def run_maintenance(dry_run: bool = False) -> dict:
    """Archive and prune old data, then release freed pages. Safe to rerun."""
    global _running
    if _running:
        return {"skipped": True, "reason": "Maintenance is already running."}
    _running = True
    try:
        return await _run(dry_run)
    finally:
        _running = False


async def _run(dry_run: bool) -> dict:
    cutoff = cutoff_modifier()
    size_before = os.path.getsize(db_path())
    async with get_db() as db:
        summary = await _counts(db, cutoff)
        summary.update(retention_days=retention_days(), dry_run=dry_run)
        if dry_run:
            return summary

        await db.execute("ATTACH DATABASE ? AS archive", (archive_path(),))
        try:
            post_cols = await _ensure_archive_table(db, "posts")
            comment_cols = await _ensure_archive_table(db, "generated_comments")
            await db.execute(
                "CREATE TABLE IF NOT EXISTS archive.handle_profiles ("
                "handle_id INTEGER PRIMARY KEY, linkedin_handle TEXT, "
                "enrichment_json TEXT, enriched_at TEXT, "
                "archived_at TEXT DEFAULT (datetime('now')))"
            )

            # One transaction across both files (rollback-journal mode commits
            # attached databases atomically), so nothing is deleted from the
            # main DB unless its archive copy is written too.
            await db.execute(
                f"UPDATE posts AS p SET status = 'dismissed' "
                f"WHERE p.status = 'unreviewed' AND {_OLD_POST}",
                (cutoff,),
            )
            pc = ", ".join(post_cols)
            await db.execute(
                f"INSERT OR IGNORE INTO archive.posts ({pc}) "
                f"SELECT {pc} FROM main.posts p WHERE {_OLD_POST}",
                (cutoff,),
            )
            cc = ", ".join(comment_cols)
            gc = ", ".join(f"g.{c}" for c in comment_cols)
            await db.execute(
                f"INSERT OR IGNORE INTO archive.generated_comments ({cc}) "
                f"SELECT {gc} FROM main.generated_comments g "
                f"JOIN main.posts p ON g.post_id = p.id "
                f"WHERE {_OLD_POST} AND {_UNPOSTED_COMMENT}",
                (cutoff,),
            )
            await db.execute(
                "DELETE FROM generated_comments AS g WHERE g.post_id IN "
                f"(SELECT p.id FROM posts p WHERE {_OLD_POST}) AND {_UNPOSTED_COMMENT}",
                (cutoff,),
            )

            cur = await db.execute(
                f"SELECT p.id, p.engagement_json FROM posts p WHERE {_OLD_POST} "
                "AND p.engagement_json LIKE '%\"raw\"%'",
                (cutoff,),
            )
            for row in await cur.fetchall():
                try:
                    slim = slim_engagement(row["engagement_json"])
                except (json.JSONDecodeError, TypeError, AttributeError) as e:
                    logger.warning("Post %d: unreadable engagement_json, left as is: %s", row["id"], e)
                    continue
                await db.execute(
                    "UPDATE posts SET engagement_json = ? WHERE id = ?", (slim, row["id"])
                )

            await db.execute(
                "INSERT OR IGNORE INTO archive.handle_profiles "
                "(handle_id, linkedin_handle, enrichment_json, enriched_at) "
                "SELECT id, linkedin_handle, enrichment_json, enriched_at FROM main.handles "
                "WHERE enrichment_json IS NOT NULL"
            )
            await db.execute(
                "UPDATE handles SET enrichment_json = NULL WHERE enrichment_json IS NOT NULL"
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.execute("DETACH DATABASE archive")

        # No-op until the file has been switched to incremental auto-vacuum.
        cur = await db.execute("PRAGMA incremental_vacuum")
        await cur.fetchall()

    summary["size_before"] = size_before
    summary["size_after"] = os.path.getsize(db_path())
    logger.info("Maintenance: %s", summary)
    return summary


def full_vacuum() -> tuple[int, int]:
    """Rebuild the file and switch it to incremental auto-vacuum. Takes an
    exclusive lock for the duration, so run it with the app stopped."""
    before = os.path.getsize(db_path())
    conn = sqlite3.connect(db_path(), isolation_level=None)
    try:
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("VACUUM")
    finally:
        conn.close()
    return before, os.path.getsize(db_path())


def _mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    ap.add_argument("--vacuum", action="store_true", help="full VACUUM after pruning")
    args = ap.parse_args()

    setup_logging()
    await init_db()

    s = await run_maintenance(dry_run=args.dry_run)
    verb = "Would" if args.dry_run else "Did"
    print(
        f"{verb}: dismiss {s['dismissed']} old unreviewed post(s), "
        f"drop {s['comments_dropped']} unposted comment(s), "
        f"slim {s['posts_slimmed']} post blob(s), "
        f"clear {s['profiles_cleared']} handle profile(s) "
        f"(retention {s['retention_days']} days)."
    )
    if args.dry_run:
        return 0
    print(f"Archive: {archive_path()}")
    if args.vacuum:
        before, after = full_vacuum()
        print(f"VACUUM: {_mb(before)} -> {_mb(after)}")
    else:
        print(f"Size: {_mb(s['size_before'])} -> {_mb(s['size_after'])}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
