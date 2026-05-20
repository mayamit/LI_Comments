import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from agent import get_last_run, run_fetch
from database import get_db
from utils import relative_time as _relative_time

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="templates")

HANDLE_RE = re.compile(r"^[a-zA-Z0-9-]{1,100}$")


def _is_stale(iso: Optional[str]) -> bool:
    if not iso:
        return True
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() > 86_400


async def _fetch_handles():
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT h.id, h.linkedin_handle, h.display_name, h.active, h.notes,
                   h.last_fetched_at,
                   COUNT(pl.id) AS posts_count,
                   AVG(pl.rating) AS avg_rating,
                   COUNT(pl.rating) AS rated_count
            FROM handles h
            LEFT JOIN posts p ON p.handle_id = h.id
            LEFT JOIN posted_log pl ON pl.post_id = p.id
            WHERE h.deleted_at IS NULL
            GROUP BY h.id
            ORDER BY h.linkedin_handle
            """
        )
        rows = await cur.fetchall()
    return [
        {
            "id": r["id"],
            "linkedin_handle": r["linkedin_handle"],
            "display_name": r["display_name"],
            "active": bool(r["active"]),
            "notes": r["notes"],
            "last_fetched_at": r["last_fetched_at"],
            "last_fetched_display": _relative_time(r["last_fetched_at"]),
            "is_stale": _is_stale(r["last_fetched_at"]),
            "posts_count": r["posts_count"] or 0,
            "avg_rating": round(r["avg_rating"], 1) if r["avg_rating"] is not None else None,
            "rated_count": r["rated_count"] or 0,
        }
        for r in rows
    ]


async def _render(
    request: Request,
    *,
    full_page: bool,
    edit_id: Optional[int] = None,
    error: Optional[str] = None,
    flash: Optional[str] = None,
):
    handles = await _fetch_handles()
    last_run = await get_last_run()
    if last_run:
        last_run["started_display"] = _relative_time(last_run["started_at"])
    ctx = {
        "handles": handles,
        "edit_id": edit_id,
        "error": error,
        "flash": flash,
        "last_run": last_run,
    }
    template = "admin.html" if full_page else "_admin_main.html"
    return templates.TemplateResponse(request, template, ctx)


@router.get("", response_class=HTMLResponse)
async def admin_page(request: Request, edit: Optional[int] = None):
    return await _render(request, full_page=True, edit_id=edit)


@router.post("/handles", response_class=HTMLResponse)
async def add_handle(
    request: Request,
    linkedin_handle: str = Form(...),
    display_name: str = Form(""),
    notes: str = Form(""),
):
    handle = linkedin_handle.strip()
    display = display_name.strip() or None
    note = notes.strip() or None

    if not handle:
        return await _render(request, full_page=False, error="Handle is required.")
    if not HANDLE_RE.match(handle):
        return await _render(
            request,
            full_page=False,
            error=f"Invalid handle '{handle}'. Use letters, numbers, and hyphens only.",
        )

    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, deleted_at FROM handles WHERE linkedin_handle = ?", (handle,)
        )
        existing = await cur.fetchone()
        if existing and existing["deleted_at"] is None:
            return await _render(
                request, full_page=False, error=f"Handle '{handle}' already exists."
            )
        if existing and existing["deleted_at"] is not None:
            await db.execute(
                "UPDATE handles SET deleted_at = NULL, active = 1, display_name = ?, notes = ? WHERE id = ?",
                (display, note, existing["id"]),
            )
            await db.commit()
            return await _render(
                request, full_page=False, flash=f"Restored handle '{handle}'."
            )
        await db.execute(
            "INSERT INTO handles (linkedin_handle, display_name, notes, active) VALUES (?, ?, ?, 1)",
            (handle, display, note),
        )
        await db.commit()
    return await _render(request, full_page=False, flash=f"Added handle '{handle}'.")


@router.get("/handles/{handle_id}/edit", response_class=HTMLResponse)
async def edit_handle_form(request: Request, handle_id: int):
    return await _render(request, full_page=False, edit_id=handle_id)


@router.post("/handles/{handle_id}/edit", response_class=HTMLResponse)
async def save_handle_edit(
    request: Request,
    handle_id: int,
    display_name: str = Form(""),
    notes: str = Form(""),
):
    async with get_db() as db:
        await db.execute(
            "UPDATE handles SET display_name = ?, notes = ? WHERE id = ? AND deleted_at IS NULL",
            (display_name.strip() or None, notes.strip() or None, handle_id),
        )
        await db.commit()
    return await _render(request, full_page=False, flash="Handle updated.")


@router.post("/handles/{handle_id}/delete", response_class=HTMLResponse)
async def delete_handle(request: Request, handle_id: int):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT linkedin_handle FROM handles WHERE id = ? AND deleted_at IS NULL",
            (handle_id,),
        )
        row = await cur.fetchone()
        if not row:
            return await _render(request, full_page=False, error="Handle not found.")
        await db.execute(
            "UPDATE handles SET deleted_at = datetime('now'), active = 0 WHERE id = ?",
            (handle_id,),
        )
        await db.commit()
    return await _render(
        request, full_page=False, flash=f"Deleted handle '{row['linkedin_handle']}'."
    )


@router.post("/run-now", response_class=HTMLResponse)
async def run_now(request: Request):
    summary = await run_fetch(trigger="manual")
    if summary.get("skipped"):
        return await _render(request, full_page=False, error=summary["reason"])
    parts = [
        f"{summary['handles_processed']} handles",
        f"{summary['new_posts']} new",
        f"{summary['skipped_duplicates']} duplicates",
        f"{summary.get('comments_generated', 0)} comments",
    ]
    if summary["errors"]:
        parts.append(f"{len(summary['errors'])} errors")
    return await _render(
        request, full_page=False, flash="Fetch complete — " + ", ".join(parts) + "."
    )


@router.post("/handles/{handle_id}/toggle", response_class=HTMLResponse)
async def toggle_handle(request: Request, handle_id: int):
    async with get_db() as db:
        await db.execute(
            "UPDATE handles SET active = 1 - active WHERE id = ? AND deleted_at IS NULL",
            (handle_id,),
        )
        await db.commit()
    return await _render(request, full_page=False)
