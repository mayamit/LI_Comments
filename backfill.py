"""Backfill comments (and optionally summaries) for posts that have none.

Use after a run where comment generation failed wholesale — e.g. the `claude`
CLI was logged out — so the posts are in the DB but have no comments. Fetching
again would not help: the posts are already stored and dedup skips them.

    python backfill.py                     # comments for unreviewed posts
    python backfill.py --status any        # ...regardless of status
    python backfill.py --summaries         # also backfill missing TL;DRs
    python backfill.py --dry-run           # just list what would run
"""
import argparse
import asyncio
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

from comments import generate_for_post, generate_summary_for_post  # noqa: E402
from database import get_db, init_db  # noqa: E402
from logging_setup import setup_logging  # noqa: E402
from maintenance import cutoff_modifier  # noqa: E402

logger = logging.getLogger("backfill")


async def _posts_missing_comments(status: str) -> list[dict]:
    sql = (
        "SELECT p.id, p.status, "
        "COALESCE(h.linkedin_handle, p.author_handle) AS handle "
        "FROM posts p "
        "LEFT JOIN handles h ON p.handle_id = h.id "
        "LEFT JOIN generated_comments g ON g.post_id = p.id "
        "WHERE g.id IS NULL AND p.status != 'posted' "
        # Past retention, maintenance deletes unposted comments on purpose.
        "AND p.fetched_at >= datetime('now', ?) "
    )
    params: tuple = (cutoff_modifier(),)
    if status != "any":
        sql += "AND p.status = ? "
        params += (status,)
    sql += "ORDER BY p.id"
    async with get_db() as db:
        cur = await db.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]


async def _posts_missing_summaries(status: str) -> list[dict]:
    sql = (
        "SELECT id, status FROM posts "
        "WHERE (summary IS NULL OR summary = '') "
        "AND content IS NOT NULL AND content != '' "
    )
    params: tuple = ()
    if status != "any":
        sql += "AND status = ? "
        params = (status,)
    sql += "ORDER BY id"
    async with get_db() as db:
        cur = await db.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--status",
        default="unreviewed",
        help="only posts with this status, or 'any' (default: unreviewed). "
        "'posted' posts are never touched.",
    )
    ap.add_argument(
        "--summaries",
        action="store_true",
        help="also backfill missing post summaries (one extra CLI call each)",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="list the work, change nothing"
    )
    args = ap.parse_args()

    setup_logging()
    await init_db()

    posts = await _posts_missing_comments(args.status)
    summaries = await _posts_missing_summaries(args.status) if args.summaries else []

    print(f"{len(posts)} post(s) missing comments" + (
        f", {len(summaries)} missing summaries" if args.summaries else ""
    ))
    if args.dry_run:
        for p in posts:
            print(f"  comments: post {p['id']} ({p['status']}) @{p['handle']}")
        for s in summaries:
            print(f"  summary:  post {s['id']} ({s['status']})")
        return 0

    # Sequential on purpose: generate_for_post already fans out across tones,
    # and the CLI semaphore caps real concurrency anyway.
    ok = failed = 0
    for i, s in enumerate(summaries, 1):
        try:
            await generate_summary_for_post(s["id"])
            logger.info("Summary %d/%d: post %d ok", i, len(summaries), s["id"])
        except Exception as e:
            failed += 1
            logger.error("Summary for post %d failed: %s", s["id"], e)

    for i, p in enumerate(posts, 1):
        try:
            res = await generate_for_post(p["id"])
            ok += res["generated"]
            failed += len(res["errors"])
            logger.info(
                "Comments %d/%d: post %d -> %d ok, %d skipped, %d errors",
                i, len(posts), p["id"], res["generated"], res["skipped"],
                len(res["errors"]),
            )
        except Exception as e:
            failed += 1
            logger.error("Comment generation for post %d failed: %s", p["id"], e)

    print(f"Done: {ok} comment(s) generated, {failed} failure(s).")
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
