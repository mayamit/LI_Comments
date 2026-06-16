from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from comments import generate_for_post
from database import get_db
from discover import (
    DiscoveryError,
    _keep_top_n,
    _window,
    get_active_topics,
    get_last_discovery_run,
    promote_author,
    run_discovery,
)
from routers.dashboard import _fetch_posts, _post_status, _set_status_if

router = APIRouter(prefix="/discover", tags=["discover"])
templates = Jinja2Templates(directory="templates")


async def _render(
    request: Request,
    *,
    full_page: bool,
    flash: Optional[str] = None,
    error: Optional[str] = None,
    undo_log_id: Optional[int] = None,
):
    # Trending posts are ranked by engagement, not recency.
    posts = await _fetch_posts("all", source="trending", order="engagement")
    topics = await get_active_topics()
    last_run = await get_last_discovery_run()
    ctx = {
        "posts": posts,
        "topics": topics,
        "window": _window(),
        "keep_top_n": _keep_top_n(),
        "last_run": last_run,
        "active_status": "unreviewed",  # for the shared post-card actions
        "post_action_base": "/discover",  # route card actions back to this view
        "flash": flash,
        "error": error,
        "undo_log_id": undo_log_id,
    }
    template = "discover.html" if full_page else "_discover_main.html"
    return templates.TemplateResponse(request, template, ctx)


@router.get("", response_class=HTMLResponse)
async def discover(request: Request):
    is_htmx = request.headers.get("hx-request") == "true"
    return await _render(request, full_page=not is_htmx)


@router.post("/run", response_class=HTMLResponse)
async def run_now(request: Request):
    summary = await run_discovery(trigger="manual")
    if summary.get("skipped"):
        return await _render(request, full_page=False, error=summary["reason"])
    parts = [
        f"{summary['posts_found']} found",
        f"{summary['posts_kept']} kept",
        f"{summary['skipped_duplicates']} duplicates",
        f"{summary.get('comments_generated', 0)} comments",
    ]
    msg = "Discovery complete — " + ", ".join(parts) + "."
    if summary["errors"]:
        msg += f" {len(summary['errors'])} errors (see logs)."
    return await _render(request, full_page=False, flash=msg)


@router.post("/posts/{post_id}/promote-author", response_class=HTMLResponse)
async def promote(request: Request, post_id: int):
    try:
        result = await promote_author(post_id)
    except DiscoveryError as e:
        return await _render(request, full_page=False, error=str(e))
    verb = "added" if result["created"] else "re-activated"
    return await _render(
        request,
        full_page=False,
        flash=f"{result['handle']} {verb} to your monitored handles.",
    )


@router.post("/posts/{post_id}/dismiss", response_class=HTMLResponse)
async def dismiss_post(request: Request, post_id: int):
    await _set_status_if(post_id, "dismissed")
    return await _render(request, full_page=False, flash="Post dismissed.")


@router.post("/posts/{post_id}/regenerate", response_class=HTMLResponse)
async def regenerate_all(request: Request, post_id: int):
    if await _post_status(post_id) == "posted":
        return await _render(request, full_page=False, error="Cannot regenerate a posted post.")
    try:
        summary = await generate_for_post(post_id)
    except Exception as e:
        return await _render(request, full_page=False, error=f"Regeneration failed: {e}")
    msg = (
        f"Regenerated post {post_id}: {summary['generated']} new, "
        f"{summary['skipped']} skipped"
        + (f", {len(summary['errors'])} errors" if summary["errors"] else "")
    )
    return await _render(request, full_page=False, flash=msg)


@router.post("/posts/{post_id}/comments/{tone_key}/mark-posted", response_class=HTMLResponse)
async def mark_posted(request: Request, post_id: int, tone_key: str):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id FROM generated_comments WHERE post_id = ? AND tone = ?",
            (post_id, tone_key),
        )
        comment = await cur.fetchone()
        if not comment:
            return await _render(
                request, full_page=False,
                error=f"No comment found for tone '{tone_key}' on this post.",
            )
        await db.execute("DELETE FROM posted_log WHERE post_id = ?", (post_id,))
        cur = await db.execute(
            "INSERT INTO posted_log (post_id, comment_id, tone) VALUES (?, ?, ?)",
            (post_id, comment["id"], tone_key),
        )
        log_id = cur.lastrowid
        await db.execute("UPDATE posts SET status = 'posted' WHERE id = ?", (post_id,))
        await db.commit()
    return await _render(request, full_page=False, undo_log_id=log_id)


@router.post("/posted/{log_id}/undo", response_class=HTMLResponse)
async def undo_mark_posted(request: Request, log_id: int):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT post_id, posted_at FROM posted_log WHERE id = ?", (log_id,)
        )
        row = await cur.fetchone()
    if not row:
        return await _render(request, full_page=False, error="Already undone or expired.")
    try:
        posted_at = datetime.fromisoformat(row["posted_at"].replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        posted_at = datetime.now(timezone.utc)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - posted_at).total_seconds() > 10:
        return await _render(request, full_page=False, error="Undo window expired (10 seconds).")
    async with get_db() as db:
        await db.execute("DELETE FROM posted_log WHERE id = ?", (log_id,))
        await db.execute(
            "UPDATE posts SET status = 'reviewed' WHERE id = ?", (row["post_id"],)
        )
        await db.commit()
    return await _render(request, full_page=False, flash="Undone.")
