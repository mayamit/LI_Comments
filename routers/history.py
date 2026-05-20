from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import tones as tones_store
from database import get_db
from utils import relative_time, truncate

router = APIRouter(prefix="/history", tags=["history"])
templates = Jinja2Templates(directory="templates")


async def _fetch_tone_breakdown() -> list[dict]:
    """Per-tone count of posted comments and average rating where rated."""
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT tone,
                   COUNT(*) AS posts,
                   AVG(rating) AS avg_rating,
                   COUNT(rating) AS rated_count
            FROM posted_log
            GROUP BY tone
            ORDER BY (avg_rating IS NULL), avg_rating DESC, posts DESC
            """
        )
        rows = await cur.fetchall()
    breakdown = []
    for r in rows:
        tone_meta = tones_store.get(r["tone"]) or {}
        breakdown.append(
            {
                "tone": r["tone"],
                "name": tone_meta.get("name", r["tone"]),
                "posts": r["posts"],
                "avg_rating": round(r["avg_rating"], 2) if r["avg_rating"] is not None else None,
                "rated_count": r["rated_count"] or 0,
            }
        )
    return breakdown


async def _fetch_stats() -> dict:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT COUNT(*) AS c FROM posts WHERE status IN ('reviewed', 'posted')"
        )
        posts_reviewed = (await cur.fetchone())["c"]

        cur = await db.execute("SELECT COUNT(*) AS c FROM posted_log")
        comments_posted = (await cur.fetchone())["c"]

        cur = await db.execute(
            "SELECT tone, COUNT(*) AS c FROM posted_log GROUP BY tone "
            "ORDER BY c DESC LIMIT 1"
        )
        row = await cur.fetchone()
        most_used_tone = {"tone": row["tone"], "count": row["c"]} if row else None

        cur = await db.execute(
            "SELECT COUNT(*) AS c FROM posted_log "
            "WHERE posted_at >= datetime('now', '-7 days')"
        )
        this_week = (await cur.fetchone())["c"]

        cur = await db.execute(
            "SELECT COUNT(*) AS c FROM posted_log "
            "WHERE posted_at >= datetime('now', '-14 days') "
            "AND posted_at < datetime('now', '-7 days')"
        )
        last_week = (await cur.fetchone())["c"]

        cur = await db.execute(
            """
            SELECT h.linkedin_handle, h.display_name, COUNT(*) AS c
            FROM posted_log pl
            JOIN posts p ON pl.post_id = p.id
            JOIN handles h ON p.handle_id = h.id
            GROUP BY h.id
            ORDER BY c DESC, h.linkedin_handle ASC
            """
        )
        per_handle = [dict(r) for r in await cur.fetchall()]

    # Translate tone key → display name
    if most_used_tone:
        t = tones_store.get(most_used_tone["tone"])
        most_used_tone["name"] = t["name"] if t else most_used_tone["tone"]

    return {
        "posts_reviewed": posts_reviewed,
        "comments_posted": comments_posted,
        "most_used_tone": most_used_tone,
        "this_week": this_week,
        "last_week": last_week,
        "per_handle": per_handle,
    }


async def _fetch_history(handle: Optional[str], date_from: Optional[str], date_to: Optional[str]) -> list[dict]:
    where, params = [], []
    if handle:
        where.append("h.linkedin_handle = ?")
        params.append(handle)
    if date_from:
        where.append("date(pl.posted_at) >= date(?)")
        params.append(date_from)
    if date_to:
        where.append("date(pl.posted_at) <= date(?)")
        params.append(date_to)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    async with get_db() as db:
        cur = await db.execute(
            f"""
            SELECT pl.id AS log_id, pl.tone, pl.posted_at, pl.notes,
                   p.id AS post_id, p.content AS post_content, p.url AS post_url,
                   h.linkedin_handle, h.display_name, h.deleted_at AS handle_deleted_at,
                   gc.content AS comment_content
            FROM posted_log pl
            JOIN posts p ON pl.post_id = p.id
            JOIN handles h ON p.handle_id = h.id
            LEFT JOIN generated_comments gc ON pl.comment_id = gc.id
            {where_clause}
            ORDER BY pl.posted_at DESC, pl.id DESC
            """,
            params,
        )
        rows = await cur.fetchall()

    history = []
    for r in rows:
        tone_meta = tones_store.get(r["tone"]) or {}
        history.append(
            {
                "log_id": r["log_id"],
                "tone": r["tone"],
                "tone_name": tone_meta.get("name", r["tone"]),
                "posted_at": r["posted_at"],
                "posted_at_display": relative_time(r["posted_at"]),
                "handle": r["linkedin_handle"],
                "display_name": r["display_name"] or r["linkedin_handle"],
                "handle_deleted": r["handle_deleted_at"] is not None,
                "post_preview": truncate(r["post_content"], 240),
                "post_content": r["post_content"],
                "post_url": r["post_url"],
                "comment_content": r["comment_content"],
            }
        )
    return history


async def _fetch_handle_options() -> list[dict]:
    """Distinct handles that have at least one posted_log entry."""
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT DISTINCT h.linkedin_handle, h.display_name
            FROM posted_log pl
            JOIN posts p ON pl.post_id = p.id
            JOIN handles h ON p.handle_id = h.id
            ORDER BY h.linkedin_handle
            """
        )
        return [dict(r) for r in await cur.fetchall()]


@router.get("", response_class=HTMLResponse)
async def history_page(
    request: Request,
    handle: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    handle = handle or None
    date_from = date_from or None
    date_to = date_to or None
    stats = await _fetch_stats()
    tone_breakdown = await _fetch_tone_breakdown()
    rows = await _fetch_history(handle, date_from, date_to)
    handle_options = await _fetch_handle_options()
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "stats": stats,
            "tone_breakdown": tone_breakdown,
            "rows": rows,
            "handle_options": handle_options,
            "filter_handle": handle or "",
            "filter_from": date_from or "",
            "filter_to": date_to or "",
        },
    )
