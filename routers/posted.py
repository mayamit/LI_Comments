"""Compact 'who did I post to and when' table view.

Same data source as /history but stripped down to a scannable table
with date filtering. /history keeps the stats and expandable detail rows;
this page is for the quick "did I already post to X?" check.
"""
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import tones as tones_store
from database import get_db
from utils import relative_time

router = APIRouter(prefix="/posted", tags=["posted"])
templates = Jinja2Templates(directory="templates")


async def _fetch_handle_options() -> list[dict]:
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


async def _fetch_rows(
    handle: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
) -> list[dict]:
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
            SELECT pl.id AS log_id, pl.tone, pl.posted_at, pl.rating,
                   h.linkedin_handle, h.display_name,
                   h.deleted_at AS handle_deleted_at,
                   p.url AS post_url
            FROM posted_log pl
            JOIN posts p ON pl.post_id = p.id
            JOIN handles h ON p.handle_id = h.id
            {where_clause}
            ORDER BY pl.posted_at DESC, pl.id DESC
            """,
            params,
        )
        rows = await cur.fetchall()

    out = []
    for r in rows:
        tone_meta = tones_store.get(r["tone"]) or {}
        out.append(
            {
                "log_id": r["log_id"],
                "tone": r["tone"],
                "tone_name": tone_meta.get("name", r["tone"]),
                "posted_at": r["posted_at"],
                "posted_at_display": relative_time(r["posted_at"]),
                "handle": r["linkedin_handle"],
                "display_name": r["display_name"] or r["linkedin_handle"],
                "handle_deleted": r["handle_deleted_at"] is not None,
                "post_url": r["post_url"],
                "rating": r["rating"],
            }
        )
    return out


def _unique_handles_count(rows: list[dict]) -> int:
    return len({r["handle"] for r in rows})


@router.post("/{log_id}/rate", response_class=HTMLResponse)
async def rate(request: Request, log_id: int, rating: int = Form(...)):
    if rating < 0 or rating > 5:
        raise HTTPException(400, "rating must be 0-5 (0 clears)")
    new_value = rating if rating > 0 else None
    async with get_db() as db:
        cur = await db.execute("SELECT id FROM posted_log WHERE id = ?", (log_id,))
        if not await cur.fetchone():
            raise HTTPException(404, "posted_log row not found")
        await db.execute(
            "UPDATE posted_log SET rating = ?, rated_at = datetime('now') WHERE id = ?",
            (new_value, log_id),
        )
        await db.commit()
    return templates.TemplateResponse(
        request, "_rating.html", {"log_id": log_id, "rating": new_value}
    )


@router.get("", response_class=HTMLResponse)
async def posted_page(
    request: Request,
    handle: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    handle = handle or None
    date_from = date_from or None
    date_to = date_to or None
    rows = await _fetch_rows(handle, date_from, date_to)
    handle_options = await _fetch_handle_options()
    return templates.TemplateResponse(
        request,
        "posted.html",
        {
            "rows": rows,
            "unique_handles": _unique_handles_count(rows),
            "handle_options": handle_options,
            "filter_handle": handle or "",
            "filter_from": date_from or "",
            "filter_to": date_to or "",
        },
    )
