"""Trending post discovery.

Searches LinkedIn for the highest-engagement posts on configured topics within a
time window (via the Apify `linkedin-post-search` actor), ranks them by
engagement, stores the top N as `posts` rows with `source='trending'`, and runs
them through the existing summary + 6-tone comment pipeline.

Unlike the monitored fetch, trending posts are NOT tied to a handles row — the
author is stored inline on the post (`author_handle`/`author_name`). A handles
row is only created when the user promotes the author from the discovery view.
"""
import asyncio
import json
import logging
import os
from typing import Any, Optional

import httpx

from agent import (
    _extract_content,
    _extract_post_id,
    _extract_posted_at,
    _extract_url,
    _normalize_content,
    _to_int,
)
from database import get_db

logger = logging.getLogger(__name__)

POST_SEARCH_ACTOR = "harvestapi~linkedin-post-search"
APIFY_BASE = "https://api.apify.com/v2"
RUN_TIMEOUT_S = 300

# Single-process re-entrancy guard (separate from the monitored fetch's).
_discovering = False


class DiscoveryError(Exception):
    pass


# --------------------------- config ---------------------------


def _window() -> str:
    return os.getenv("DISCOVERY_WINDOW", "week")


def _max_posts_per_query() -> int:
    return int(os.getenv("DISCOVERY_MAX_POSTS_PER_QUERY", "25"))


def _keep_top_n() -> int:
    return int(os.getenv("DISCOVERY_KEEP_TOP_N", "10"))


async def get_active_topics() -> list[str]:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT query FROM discovery_topics WHERE active = 1 ORDER BY id"
        )
        rows = await cur.fetchall()
    return [r[0] for r in rows]


# --------------------------- actor call ---------------------------


async def post_search(queries: list[str], window: str, max_posts: int) -> list[dict]:
    """Run the post-search actor for the given queries and return raw items."""
    token = os.getenv("APIFY_TOKEN")
    if not token:
        raise DiscoveryError("APIFY_TOKEN not set")
    if not queries:
        return []
    url = (
        f"{APIFY_BASE}/acts/{POST_SEARCH_ACTOR}/run-sync-get-dataset-items"
        f"?token={token}&timeout={RUN_TIMEOUT_S}"
    )
    body = {
        "searchQueries": queries,
        "postedLimit": window,
        "maxPosts": max_posts,
        "sortBy": "relevance",
    }
    async with httpx.AsyncClient(timeout=RUN_TIMEOUT_S + 20) as client:
        r = await client.post(url, json=body)
    if r.status_code >= 400:
        raise DiscoveryError(
            f"Apify post-search actor returned {r.status_code}: {r.text[:200]}"
        )
    items = r.json()
    return items if isinstance(items, list) else []


# --------------------------- ranking + extraction ---------------------------


def _extract_engagement(item: dict) -> dict:
    """Pull reactions/comments/reposts counts from a post-search item.

    The actor nests counts under `engagement` ({likes, comments, shares}); fall
    back to the flat field names the monitored fetch already handles.
    """
    eng = item.get("engagement") if isinstance(item.get("engagement"), dict) else {}
    reactions = (
        _to_int(eng.get("likes"))
        or _to_int(eng.get("reactions"))
        or _to_int(item.get("reactionsCount"))
        or _to_int(item.get("likes"))
    )
    comments = (
        _to_int(eng.get("comments"))
        or _to_int(item.get("commentsCount"))
        or _to_int(item.get("comments"))
    )
    reposts = (
        _to_int(eng.get("shares"))
        or _to_int(eng.get("reposts"))
        or _to_int(item.get("repostsCount"))
        or _to_int(item.get("reposts"))
    )
    return {
        "reactions": reactions or 0,
        "comments": comments or 0,
        "reposts": reposts or 0,
    }


def score_engagement(item: dict) -> int:
    """Rank key: weight comments and reposts above reactions (stronger signals)."""
    e = _extract_engagement(item)
    return e["reactions"] + 2 * e["comments"] + 3 * e["reposts"]


def _extract_author(item: dict) -> dict:
    a = item.get("author") if isinstance(item.get("author"), dict) else {}
    handle = a.get("publicIdentifier") or a.get("universalName")
    name = a.get("name")
    info = a.get("info")
    # Use the headline as a richer display name when present.
    display = f"{name} — {info}" if name and info else (name or handle)
    return {"handle": handle, "name": display}


# --------------------------- persistence ---------------------------


async def _insert_trending_post_if_new(item: dict) -> Optional[int]:
    """Insert a trending post if its post_id is new. Returns the new id or None."""
    post_id = _extract_post_id(item)
    if not post_id:
        return None
    async with get_db() as db:
        cur = await db.execute("SELECT 1 FROM posts WHERE post_id = ?", (post_id,))
        if await cur.fetchone():
            return None
        content = _normalize_content(_extract_content(item))
        url = _extract_url(item)
        posted_at = _extract_posted_at(item)
        author = _extract_author(item)
        eng = _extract_engagement(item)
        engagement_json = json.dumps({**eng, "raw": item}, default=str)
        cur = await db.execute(
            "INSERT INTO posts (handle_id, post_id, content, url, engagement_json, "
            "posted_at, source, engagement_score, author_handle, author_name) "
            "VALUES (NULL, ?, ?, ?, ?, ?, 'trending', ?, ?, ?)",
            (
                post_id,
                content,
                url,
                engagement_json,
                posted_at,
                score_engagement(item),
                author["handle"],
                author["name"],
            ),
        )
        await db.commit()
        return cur.lastrowid


async def _now_iso() -> str:
    async with get_db() as db:
        cur = await db.execute("SELECT datetime('now')")
        row = await cur.fetchone()
        return row[0]


# --------------------------- orchestration ---------------------------


async def run_discovery(trigger: str = "manual") -> dict:
    """Discover trending posts for the active topics, rank, store the top N, and
    generate summaries + comments. Returns a summary dict. Reuses the monitored
    fetch's convention: per-post failures are logged and never abort the run."""
    global _discovering
    if _discovering:
        return {"skipped": True, "reason": "A discovery run is already in progress."}
    _discovering = True
    try:
        return await _run_discovery_inner(trigger)
    finally:
        _discovering = False


async def _run_discovery_inner(trigger: str) -> dict:
    from comments import generate_for_post, generate_summary_for_post

    queries = await get_active_topics()
    if not queries:
        return {"skipped": True, "reason": "No active discovery topics configured."}

    window = _window()
    started_at = await _now_iso()
    summary: dict[str, Any] = {
        "trigger": trigger,
        "started_at": started_at,
        "queries": queries,
        "window": window,
        "posts_found": 0,
        "posts_kept": 0,
        "skipped_duplicates": 0,
        "comments_generated": 0,
        "summaries_generated": 0,
        "errors": [],
    }

    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO discovery_runs (trigger, queries, window, started_at) "
            "VALUES (?, ?, ?, ?)",
            (trigger, json.dumps(queries), window, started_at),
        )
        run_id = cur.lastrowid
        await db.commit()

    try:
        items = await post_search(queries, window, _max_posts_per_query())
    except DiscoveryError as e:
        logger.warning("Discovery search failed: %s", e)
        summary["errors"].append({"query": ",".join(queries), "error": str(e)})
        items = []

    summary["posts_found"] = len(items)
    # Rank by engagement and keep the top N.
    items.sort(key=score_engagement, reverse=True)
    top = items[: _keep_top_n()]

    for item in top:
        try:
            new_id = await _insert_trending_post_if_new(item)
            if new_id is None:
                summary["skipped_duplicates"] += 1
                continue
            summary["posts_kept"] += 1
            try:
                if await generate_summary_for_post(new_id):
                    summary["summaries_generated"] += 1
            except Exception as e:
                logger.exception("Summary generation failed for trending post %d", new_id)
                summary["errors"].append({"post_id": new_id, "error": f"summary: {e}"})
            try:
                gen = await generate_for_post(new_id)
                summary["comments_generated"] += gen["generated"]
                for err in gen["errors"]:
                    summary["errors"].append(
                        {"post_id": new_id, "error": f"comment ({err['tone']}): {err['error']}"}
                    )
            except Exception as e:
                logger.exception("Comment generation failed for trending post %d", new_id)
                summary["errors"].append({"post_id": new_id, "error": f"comments: {e}"})
        except Exception as e:
            logger.exception("Discovery failed for an item")
            summary["errors"].append({"error": str(e)})

    ended_at = await _now_iso()
    summary["ended_at"] = ended_at
    summary["run_id"] = run_id

    async with get_db() as db:
        await db.execute(
            "UPDATE discovery_runs SET ended_at = ?, posts_found = ?, posts_kept = ?, "
            "skipped_duplicates = ?, error_count = ?, summary_json = ? WHERE id = ?",
            (
                ended_at,
                summary["posts_found"],
                summary["posts_kept"],
                summary["skipped_duplicates"],
                len(summary["errors"]),
                json.dumps(
                    {
                        "errors": summary["errors"],
                        "comments_generated": summary["comments_generated"],
                        "summaries_generated": summary["summaries_generated"],
                    }
                ),
                run_id,
            ),
        )
        await db.commit()

    logger.info(
        "Discovery run %s done: %d found, %d kept, %d dup, %d comments, %d errors",
        trigger,
        summary["posts_found"],
        summary["posts_kept"],
        summary["skipped_duplicates"],
        summary["comments_generated"],
        len(summary["errors"]),
    )
    return summary


async def promote_author(post_id: int) -> dict:
    """Promote a trending post's author into a monitored handle.

    Creates a handles row (or reactivates an existing one with the same handle),
    then backfills handle_id onto this post and every sibling trending post from
    the same author. Returns {"handle": name, "created": bool}.
    """
    async with get_db() as db:
        cur = await db.execute(
            "SELECT author_handle, author_name, handle_id FROM posts WHERE id = ?",
            (post_id,),
        )
        post = await cur.fetchone()
        if not post:
            raise DiscoveryError(f"Post {post_id} not found")
        if post["handle_id"] is not None:
            raise DiscoveryError("This author is already monitored.")
        author_handle = post["author_handle"]
        if not author_handle:
            raise DiscoveryError("This post has no author handle to promote.")
        author_name = post["author_name"]

        # Reuse an existing handle if one matches; otherwise create it.
        cur = await db.execute(
            "SELECT id FROM handles WHERE linkedin_handle = ?", (author_handle,)
        )
        existing = await cur.fetchone()
        if existing:
            handle_id = existing["id"]
            await db.execute(
                "UPDATE handles SET active = 1, deleted_at = NULL WHERE id = ?",
                (handle_id,),
            )
            created = False
        else:
            cur = await db.execute(
                "INSERT INTO handles (linkedin_handle, display_name, active, notes) "
                "VALUES (?, ?, 1, 'Promoted from trending discovery')",
                (author_handle, author_name),
            )
            handle_id = cur.lastrowid
            created = True

        # Link this post + sibling trending posts from the same author.
        await db.execute(
            "UPDATE posts SET handle_id = ? "
            "WHERE author_handle = ? AND source = 'trending' AND handle_id IS NULL",
            (handle_id, author_handle),
        )
        await db.commit()

    logger.info(
        "Promoted trending author @%s to monitored handle %d (created=%s)",
        author_handle,
        handle_id,
        created,
    )
    return {"handle": author_name or author_handle, "created": created}


async def get_last_discovery_run() -> Optional[dict]:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, trigger, queries, window, started_at, ended_at, posts_found, "
            "posts_kept, skipped_duplicates, error_count, summary_json "
            "FROM discovery_runs ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
    if not row:
        return None
    d = dict(row)
    parsed = json.loads(d.pop("summary_json") or "{}")
    d["errors"] = parsed.get("errors", [])
    d["comments_generated"] = parsed.get("comments_generated", 0)
    d["summaries_generated"] = parsed.get("summaries_generated", 0)
    return d
