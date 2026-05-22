import re
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from database import get_db

router = APIRouter(prefix="/admin/tags", tags=["tags"])
templates = Jinja2Templates(directory="templates")

DIMENSIONS = ("persona", "reach", "intent", "cadence")
DIMENSION_LABELS = {
    "persona": "Persona",
    "reach": "Reach",
    "intent": "Intent",
    "cadence": "Cadence",
}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")


def _slugify(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


async def _fetch_tags_grouped():
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT t.id, t.slug, t.label, t.dimension, t.description, t.sort_order,
                   COUNT(ht.handle_id) AS handle_count
            FROM tags t
            LEFT JOIN handle_tags ht ON ht.tag_id = t.id
            WHERE t.deleted_at IS NULL
            GROUP BY t.id
            ORDER BY t.dimension, t.sort_order, t.label
            """
        )
        rows = await cur.fetchall()
    grouped = {d: [] for d in DIMENSIONS}
    for r in rows:
        grouped[r["dimension"]].append(
            {
                "id": r["id"],
                "slug": r["slug"],
                "label": r["label"],
                "dimension": r["dimension"],
                "description": r["description"],
                "sort_order": r["sort_order"],
                "handle_count": r["handle_count"] or 0,
            }
        )
    return grouped


async def _render(
    request: Request,
    *,
    full_page: bool,
    edit_id: Optional[int] = None,
    error: Optional[str] = None,
    flash: Optional[str] = None,
):
    grouped = await _fetch_tags_grouped()
    ctx = {
        "grouped": grouped,
        "dimensions": DIMENSIONS,
        "dimension_labels": DIMENSION_LABELS,
        "edit_id": edit_id,
        "error": error,
        "flash": flash,
    }
    template = "tag_admin.html" if full_page else "_tag_admin_main.html"
    return templates.TemplateResponse(request, template, ctx)


@router.get("", response_class=HTMLResponse)
async def tags_page(request: Request, edit: Optional[int] = None):
    return await _render(request, full_page=True, edit_id=edit)


@router.post("", response_class=HTMLResponse)
async def create_tag(
    request: Request,
    dimension: str = Form(...),
    label: str = Form(...),
    description: str = Form(""),
    sort_order: int = Form(0),
):
    label_clean = label.strip()
    if not label_clean:
        return await _render(request, full_page=False, error="Label is required.")
    if dimension not in DIMENSIONS:
        return await _render(request, full_page=False, error=f"Invalid dimension '{dimension}'.")
    slug = _slugify(label_clean)
    if not SLUG_RE.match(slug):
        return await _render(
            request,
            full_page=False,
            error=f"Cannot derive a valid slug from '{label_clean}'.",
        )
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, deleted_at FROM tags WHERE slug = ?", (slug,)
        )
        existing = await cur.fetchone()
        if existing and existing["deleted_at"] is None:
            return await _render(
                request, full_page=False, error=f"Tag '{slug}' already exists."
            )
        if existing and existing["deleted_at"] is not None:
            await db.execute(
                """
                UPDATE tags
                SET deleted_at = NULL, label = ?, dimension = ?, description = ?, sort_order = ?
                WHERE id = ?
                """,
                (label_clean, dimension, description.strip() or None, sort_order, existing["id"]),
            )
            await db.commit()
            return await _render(
                request, full_page=False, flash=f"Restored tag '{slug}'."
            )
        await db.execute(
            """
            INSERT INTO tags (slug, label, dimension, description, sort_order)
            VALUES (?, ?, ?, ?, ?)
            """,
            (slug, label_clean, dimension, description.strip() or None, sort_order),
        )
        await db.commit()
    return await _render(request, full_page=False, flash=f"Added tag '{slug}'.")


@router.get("/{tag_id}/edit", response_class=HTMLResponse)
async def edit_form(request: Request, tag_id: int):
    return await _render(request, full_page=False, edit_id=tag_id)


@router.post("/{tag_id}/edit", response_class=HTMLResponse)
async def save_edit(
    request: Request,
    tag_id: int,
    label: str = Form(...),
    description: str = Form(""),
    sort_order: int = Form(0),
):
    label_clean = label.strip()
    if not label_clean:
        return await _render(
            request, full_page=False, edit_id=tag_id, error="Label is required."
        )
    async with get_db() as db:
        await db.execute(
            """
            UPDATE tags SET label = ?, description = ?, sort_order = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            (label_clean, description.strip() or None, sort_order, tag_id),
        )
        await db.commit()
    return await _render(request, full_page=False, flash="Tag updated.")


@router.post("/{tag_id}/delete", response_class=HTMLResponse)
async def delete_tag(request: Request, tag_id: int):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT slug FROM tags WHERE id = ? AND deleted_at IS NULL", (tag_id,)
        )
        row = await cur.fetchone()
        if not row:
            return await _render(request, full_page=False, error="Tag not found.")
        await db.execute(
            "UPDATE tags SET deleted_at = datetime('now') WHERE id = ?", (tag_id,)
        )
        await db.execute("DELETE FROM handle_tags WHERE tag_id = ?", (tag_id,))
        await db.commit()
    return await _render(request, full_page=False, flash=f"Deleted tag '{row['slug']}'.")
