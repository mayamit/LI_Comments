import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from database import get_db

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="templates")

HANDLE_RE = re.compile(r"^[a-zA-Z0-9-]{1,100}$")


def _relative_time(iso: Optional[str]) -> str:
    if not iso:
        return "Never"
    dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return f"{max(secs, 0)}s ago"
    minutes = secs // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _is_stale(iso: Optional[str]) -> bool:
    if not iso:
        return True
    dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() > 86_400


async def _fetch_handles():
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, linkedin_handle, display_name, active, notes, last_fetched_at "
            "FROM handles WHERE deleted_at IS NULL ORDER BY linkedin_handle"
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
    ctx = {
        "handles": handles,
        "edit_id": edit_id,
        "error": error,
        "flash": flash,
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


@router.post("/handles/{handle_id}/toggle", response_class=HTMLResponse)
async def toggle_handle(request: Request, handle_id: int):
    async with get_db() as db:
        await db.execute(
            "UPDATE handles SET active = 1 - active WHERE id = ? AND deleted_at IS NULL",
            (handle_id,),
        )
        await db.commit()
    return await _render(request, full_page=False)
