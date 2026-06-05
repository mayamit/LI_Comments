import asyncio
import json
import logging
import os
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from database import get_db

logger = logging.getLogger(__name__)

APIFY_ACTOR = "harvestapi~linkedin-profile-posts"
APIFY_BASE = "https://api.apify.com/v2"
POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 300

# Single-process re-entrancy guard. Safe in single-threaded asyncio:
# check-and-set has no await between, so it's atomic for our purposes.
_running = False


class FetchError(Exception):
    pass


async def fetch_latest_post(handle: str) -> Optional[dict]:
    """Fetch the latest LinkedIn post for a handle via Apify.

    Returns the first dataset item, or None if the actor returned no posts.
    Raises FetchError on actor or HTTP failure.
    """
    token = os.getenv("APIFY_TOKEN")
    if not token:
        raise FetchError("APIFY_TOKEN not set")

    async with httpx.AsyncClient(timeout=30) as client:
        start_url = f"{APIFY_BASE}/acts/{APIFY_ACTOR}/runs?token={token}"
        start_body = {
            "targetUrls": [f"https://www.linkedin.com/in/{handle}"],
            "maxPosts": 1,
        }
        r = await client.post(start_url, json=start_body)
        r.raise_for_status()
        run = r.json()["data"]
        run_id = run["id"]
        dataset_id = run["defaultDatasetId"]

        poll_url = f"{APIFY_BASE}/actor-runs/{run_id}?token={token}"
        elapsed = 0
        status = "READY"
        while elapsed < POLL_TIMEOUT_S:
            await asyncio.sleep(POLL_INTERVAL_S)
            elapsed += POLL_INTERVAL_S
            pr = await client.get(poll_url)
            pr.raise_for_status()
            status = pr.json()["data"]["status"]
            if status == "SUCCEEDED":
                break
            if status in ("FAILED", "ABORTED", "TIMED-OUT"):
                raise FetchError(f"Apify run {status.lower()}")
        if status != "SUCCEEDED":
            raise FetchError(f"Apify run timed out after {POLL_TIMEOUT_S}s")

        items_url = f"{APIFY_BASE}/datasets/{dataset_id}/items?token={token}"
        ir = await client.get(items_url)
        ir.raise_for_status()
        items = ir.json()
        return items[0] if items else None


def _normalize_content(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    return unicodedata.normalize("NFKD", text)


def _extract_post_id(item: dict) -> Optional[str]:
    for key in ("postId", "id", "urn", "url"):
        v = item.get(key)
        if v:
            return str(v)
    return None


def _epoch_to_iso(v: float) -> Optional[str]:
    ts = v if v < 10**11 else v / 1000
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return None


def _to_iso(v: Any) -> Optional[str]:
    """Coerce Apify date values (str / number / nested dict) to an ISO string."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return _epoch_to_iso(v)
    if isinstance(v, dict):
        for nk in ("iso", "isoString", "isoDate", "datetime", "date", "value"):
            s = _to_iso(v.get(nk))
            if s:
                return s
        ts = v.get("timestamp") or v.get("ts") or v.get("epoch")
        if isinstance(ts, (int, float)):
            return _epoch_to_iso(ts)
    return None


def _to_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            return None
    if isinstance(v, dict):
        for k in ("count", "total", "value", "n"):
            n = _to_int(v.get(k))
            if n is not None:
                return n
    return None


def _to_text(v: Any) -> Optional[str]:
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    return json.dumps(v, ensure_ascii=False)


def _extract_posted_at(item: dict) -> Optional[str]:
    for key in ("postedAtIso", "publishedAt", "postedAt", "posted_at", "createdAt"):
        s = _to_iso(item.get(key))
        if s:
            return s
    return None


def _extract_url(item: dict) -> Optional[str]:
    for key in ("url", "postUrl", "linkedinUrl", "permalink"):
        v = item.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def _author_handle_from_url(url: Any) -> Optional[str]:
    if not isinstance(url, str):
        return None
    import re
    m = re.search(r"/(?:in|company)/([^/?#]+)", url)
    return m.group(1) if m else None


def _extract_content(item: dict) -> Optional[str]:
    """Main post body. Falls back to repost.content when the user reshared
    something without adding their own commentary — common on LinkedIn.
    """
    direct = _to_text(item.get("content")) or _to_text(item.get("text"))
    if direct and direct.strip():
        return direct
    repost = item.get("repost")
    if isinstance(repost, dict):
        nested = _to_text(repost.get("content")) or _to_text(repost.get("text"))
        if nested and nested.strip():
            author = repost.get("author") or {}
            author_name = (
                _to_text(author.get("name"))
                or _to_text(author.get("fullName"))
                or _author_handle_from_url(author.get("linkedinUrl"))
                or "another user"
            )
            return f"[Reshared from {author_name} without commentary]\n\n{nested}"
    return None


async def _insert_post_if_new(handle_id: int, post_id: str, item: dict) -> Optional[int]:
    """Returns the new posts.id if inserted, None if duplicate."""
    async with get_db() as db:
        cur = await db.execute("SELECT 1 FROM posts WHERE post_id = ?", (post_id,))
        if await cur.fetchone():
            return None
        content = _normalize_content(_extract_content(item))
        url = _extract_url(item)
        posted_at = _extract_posted_at(item)
        engagement_json = json.dumps(
            {
                "reactions": _to_int(item.get("reactionsCount"))
                or _to_int(item.get("likes"))
                or _to_int(item.get("numLikes")),
                "comments": _to_int(item.get("commentsCount"))
                or _to_int(item.get("comments"))
                or _to_int(item.get("numComments")),
                "reposts": _to_int(item.get("repostsCount"))
                or _to_int(item.get("reposts"))
                or _to_int(item.get("numReposts")),
                "raw": item,
            },
            default=str,
        )
        cur = await db.execute(
            "INSERT INTO posts (handle_id, post_id, content, url, engagement_json, posted_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (handle_id, post_id, content, url, engagement_json, posted_at),
        )
        await db.commit()
        return cur.lastrowid


async def _update_last_fetched(handle_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE handles SET last_fetched_at = datetime('now') WHERE id = ?",
            (handle_id,),
        )
        await db.commit()


async def _now_iso() -> str:
    async with get_db() as db:
        cur = await db.execute("SELECT datetime('now')")
        row = await cur.fetchone()
        return row[0]


async def run_fetch(trigger: str = "manual") -> dict:
    """Run the fetch agent over all active handles.

    Sequential per AC. Per-handle errors are captured and don't abort the run.
    Returns a summary dict. If another run is in progress, returns
    {"skipped": True, "reason": "..."}.
    """
    global _running
    if _running:
        return {"skipped": True, "reason": "A fetch run is already in progress."}
    _running = True
    try:
        return await _run_fetch_inner(trigger)
    finally:
        _running = False


async def _run_fetch_inner(trigger: str) -> dict:
    # local imports avoid circulars at boot
    from comments import generate_for_post, generate_summary_for_post

    started_at = await _now_iso()
    summary = {
        "trigger": trigger,
        "started_at": started_at,
        "handles_processed": 0,
        "new_posts": 0,
        "skipped_duplicates": 0,
        "comments_generated": 0,
        "summaries_generated": 0,
        "errors": [],
    }

    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO fetch_runs (trigger, started_at) VALUES (?, ?)",
            (trigger, started_at),
        )
        run_id = cur.lastrowid
        await db.commit()

    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, linkedin_handle FROM handles "
            "WHERE active = 1 AND deleted_at IS NULL ORDER BY id"
        )
        handles = [dict(r) for r in await cur.fetchall()]

    for h in handles:
        summary["handles_processed"] += 1
        handle_name = h["linkedin_handle"]
        try:
            item = await fetch_latest_post(handle_name)
            if not item:
                logger.info("No posts returned for %s", handle_name)
                await _update_last_fetched(h["id"])
                continue
            post_id = _extract_post_id(item)
            if not post_id:
                raise FetchError("post_id missing from Apify response")
            new_db_id = await _insert_post_if_new(h["id"], post_id, item)
            if new_db_id is not None:
                summary["new_posts"] += 1
                try:
                    if await generate_summary_for_post(new_db_id):
                        summary["summaries_generated"] += 1
                except Exception as e:
                    logger.exception("Summary generation failed for post %d", new_db_id)
                    summary["errors"].append(
                        {"handle": handle_name, "error": f"summary generation: {e}"}
                    )
                try:
                    gen = await generate_for_post(new_db_id)
                    summary["comments_generated"] += gen["generated"]
                    for err in gen["errors"]:
                        summary["errors"].append(
                            {"handle": handle_name, "error": f"comment ({err['tone']}): {err['error']}"}
                        )
                except Exception as e:
                    logger.exception("Comment generation failed for post %d", new_db_id)
                    summary["errors"].append(
                        {"handle": handle_name, "error": f"comment generation: {e}"}
                    )
            else:
                summary["skipped_duplicates"] += 1
            await _update_last_fetched(h["id"])
        except Exception as e:
            logger.exception("Fetch failed for %s", handle_name)
            summary["errors"].append({"handle": handle_name, "error": str(e)})

    ended_at = await _now_iso()
    summary["ended_at"] = ended_at
    summary["run_id"] = run_id

    async with get_db() as db:
        await db.execute(
            "UPDATE fetch_runs SET ended_at = ?, handles_processed = ?, new_posts = ?, "
            "skipped_duplicates = ?, error_count = ?, summary_json = ? WHERE id = ?",
            (
                ended_at,
                summary["handles_processed"],
                summary["new_posts"],
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
        "Fetch run %s done: %d handles, %d new, %d dup, %d comments, %d errors",
        trigger,
        summary["handles_processed"],
        summary["new_posts"],
        summary["skipped_duplicates"],
        summary["comments_generated"],
        len(summary["errors"]),
    )
    return summary


async def get_last_run() -> Optional[dict]:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, trigger, started_at, ended_at, handles_processed, new_posts, "
            "skipped_duplicates, error_count, summary_json FROM fetch_runs "
            "ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
    if not row:
        return None
    d = dict(row)
    raw = d.pop("summary_json") or "{}"
    parsed = json.loads(raw)
    d["errors"] = parsed.get("errors", [])
    d["comments_generated"] = parsed.get("comments_generated", 0)
    d["summaries_generated"] = parsed.get("summaries_generated", 0)
    return d
