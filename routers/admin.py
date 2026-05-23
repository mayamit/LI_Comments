import re
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from agent import get_last_run, run_fetch
from database import get_db
from enrich import enrich_untagged_handles
from utils import relative_time as _relative_time

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="templates")

HANDLE_RE = re.compile(r"^[a-zA-Z0-9-]{1,100}$")
DIMENSIONS = ("persona", "reach", "intent", "cadence")
DIMENSION_LABELS = {
    "persona": "Persona",
    "reach": "Reach",
    "intent": "Intent",
    "cadence": "Cadence",
}


def _parse_tag_param(tags: Optional[str]) -> List[str]:
    if not tags:
        return []
    return [s.strip() for s in tags.split(",") if s.strip()]


RECOMMENDATION_LIMIT = 10
EXEMPT_PERSONA_SLUGS = {"investor-vc", "investor-pe"}


def _days_since(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86_400.0


def _score_for_activation(h: dict) -> dict:
    """Return a (score, reasons) bundle for an inactive handle.

    Each reason is (delta:int, label:str). delta=0 reasons (e.g. exemptions)
    are informational only.
    """
    score = 0
    reasons: list[tuple[int, str]] = []

    by_dim: dict[str, set] = {}
    for t in h["tags"]:
        by_dim.setdefault(t["dimension"], set()).add(t["slug"])
    intent = by_dim.get("intent", set())
    persona = by_dim.get("persona", set())
    reach = by_dim.get("reach", set())
    cadence = by_dim.get("cadence", set())

    posts = h["posts_count"] or 0
    if posts > 0:
        score += 3
        reasons.append((3, f"posts:{posts}"))
        if posts >= 3:
            score += 2
            reasons.append((2, "posts≥3"))
    if h["avg_rating"] is not None:
        if h["avg_rating"] >= 4:
            score += 2
            reasons.append((2, f"rating {h['avg_rating']}★"))
        elif h["avg_rating"] >= 3:
            score += 1
            reasons.append((1, f"rating {h['avg_rating']}★"))

    if h["notes"]:
        score += 2
        reasons.append((2, "notes"))
    if h["display_name"]:
        score += 1
        reasons.append((1, "display_name"))

    if intent:
        score += 3
        reasons.append((3, sorted(intent)[0]))
    if reach & {"reach-mega", "reach-large"}:
        score += 2
        reasons.append((2, "reach-large+"))
    if persona:
        score += 1
        reasons.append((1, "persona"))
    if cadence & {"cadence-daily", "cadence-weekly"}:
        score += 1
        reasons.append((1, "cadence"))

    exempt_label = None
    if "hiring-signal" in intent:
        exempt_label = "hiring-signal"
    else:
        exempt_persona = persona & EXEMPT_PERSONA_SLUGS
        if exempt_persona:
            exempt_label = sorted(exempt_persona)[0]

    days = _days_since(h["last_posted_at"])
    if days is not None:
        if exempt_label:
            reasons.append((0, f"exempt:{exempt_label}"))
        else:
            if days <= 1:
                score -= 5
                reasons.append((-5, "posted yesterday"))
            elif days <= 3:
                score -= 3
                reasons.append((-3, f"posted {int(days)}d ago"))
            elif days <= 7:
                score -= 1
                reasons.append((-1, f"posted {int(days)}d ago"))

    return {"score": score, "reasons": reasons}


def _recommendations(all_handles: list[dict]) -> list[dict]:
    rated = []
    for h in all_handles:
        if h["active"]:
            continue
        bundle = _score_for_activation(h)
        if bundle["score"] <= 0:
            continue
        rated.append({**h, **bundle})
    rated.sort(key=lambda x: (-x["score"], x["linkedin_handle"]))
    return rated[:RECOMMENDATION_LIMIT]


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


async def _fetch_all_tags_grouped(db):
    """Return all active tags grouped by dimension, sorted within each."""
    cur = await db.execute(
        """
        SELECT id, slug, label, dimension, description, sort_order
        FROM tags
        WHERE deleted_at IS NULL
        ORDER BY dimension, sort_order, label
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
            }
        )
    return grouped


async def _fetch_handles(selected_tag_slugs: Optional[List[str]] = None):
    selected_tag_slugs = selected_tag_slugs or []
    async with get_db() as db:
        where = ["h.deleted_at IS NULL"]
        params: list = []

        if selected_tag_slugs:
            placeholders = ",".join("?" * len(selected_tag_slugs))
            cur = await db.execute(
                f"SELECT slug, dimension FROM tags "
                f"WHERE slug IN ({placeholders}) AND deleted_at IS NULL",
                selected_tag_slugs,
            )
            by_dim: dict = {}
            for r in await cur.fetchall():
                by_dim.setdefault(r["dimension"], []).append(r["slug"])
            # AND across dimensions, OR within a dimension.
            for dim, slugs in by_dim.items():
                ph = ",".join("?" * len(slugs))
                where.append(
                    f"EXISTS (SELECT 1 FROM handle_tags ht "
                    f"JOIN tags t ON t.id = ht.tag_id "
                    f"WHERE ht.handle_id = h.id AND t.slug IN ({ph}))"
                )
                params.extend(slugs)

        cur = await db.execute(
            f"""
            SELECT h.id, h.linkedin_handle, h.display_name, h.active, h.notes,
                   h.last_fetched_at,
                   COUNT(pl.id) AS posts_count,
                   AVG(pl.rating) AS avg_rating,
                   COUNT(pl.rating) AS rated_count,
                   MAX(pl.posted_at) AS last_posted_at
            FROM handles h
            LEFT JOIN posts p ON p.handle_id = h.id
            LEFT JOIN posted_log pl ON pl.post_id = p.id
            WHERE {' AND '.join(where)}
            GROUP BY h.id
            ORDER BY h.linkedin_handle
            """,
            params,
        )
        handle_rows = await cur.fetchall()

        cur = await db.execute(
            """
            SELECT ht.handle_id, t.id, t.slug, t.label, t.dimension, t.sort_order
            FROM handle_tags ht
            JOIN tags t ON t.id = ht.tag_id
            WHERE t.deleted_at IS NULL
            ORDER BY t.dimension, t.sort_order, t.label
            """
        )
        tag_rows = await cur.fetchall()

    tags_by_handle: dict = {}
    for r in tag_rows:
        tags_by_handle.setdefault(r["handle_id"], []).append(
            {
                "id": r["id"],
                "slug": r["slug"],
                "label": r["label"],
                "dimension": r["dimension"],
            }
        )

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
            "last_posted_at": r["last_posted_at"],
            "last_posted_display": _relative_time(r["last_posted_at"]) if r["last_posted_at"] else "—",
            "tags": tags_by_handle.get(r["id"], []),
            "tag_ids": {t["id"] for t in tags_by_handle.get(r["id"], [])},
        }
        for r in handle_rows
    ]


async def _render(
    request: Request,
    *,
    full_page: bool,
    edit_id: Optional[int] = None,
    error: Optional[str] = None,
    flash: Optional[str] = None,
    selected_tags: Optional[List[str]] = None,
):
    selected_tags = selected_tags or []
    handles = await _fetch_handles(selected_tags)
    # Recommendations are computed across all handles, ignoring the table filter.
    all_handles = handles if not selected_tags else await _fetch_handles([])
    recommendations = _recommendations(all_handles)
    async with get_db() as db:
        all_tags_grouped = await _fetch_all_tags_grouped(db)
    last_run = await get_last_run()
    if last_run:
        last_run["started_display"] = _relative_time(last_run["started_at"])
    ctx = {
        "handles": handles,
        "edit_id": edit_id,
        "error": error,
        "flash": flash,
        "last_run": last_run,
        "all_tags_grouped": all_tags_grouped,
        "dimensions": DIMENSIONS,
        "dimension_labels": DIMENSION_LABELS,
        "selected_tags": selected_tags,
        "selected_tags_param": ",".join(selected_tags),
        "recommendations": recommendations,
    }
    template = "admin.html" if full_page else "_admin_main.html"
    return templates.TemplateResponse(request, template, ctx)


@router.get("", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    edit: Optional[int] = None,
    tags: Optional[str] = Query(None),
):
    is_htmx = request.headers.get("hx-request") == "true"
    return await _render(
        request,
        full_page=not is_htmx,
        edit_id=edit,
        selected_tags=_parse_tag_param(tags),
    )


@router.post("/handles", response_class=HTMLResponse)
async def add_handle(
    request: Request,
    linkedin_handle: str = Form(...),
    display_name: str = Form(""),
    notes: str = Form(""),
    selected_tags: str = Form(""),
):
    sel = _parse_tag_param(selected_tags)
    handle = linkedin_handle.strip()
    display = display_name.strip() or None
    note = notes.strip() or None

    if not handle:
        return await _render(request, full_page=False, error="Handle is required.", selected_tags=sel)
    if not HANDLE_RE.match(handle):
        return await _render(
            request,
            full_page=False,
            error=f"Invalid handle '{handle}'. Use letters, numbers, and hyphens only.",
            selected_tags=sel,
        )

    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, deleted_at FROM handles WHERE linkedin_handle = ?", (handle,)
        )
        existing = await cur.fetchone()
        if existing and existing["deleted_at"] is None:
            return await _render(
                request, full_page=False, error=f"Handle '{handle}' already exists.", selected_tags=sel
            )
        if existing and existing["deleted_at"] is not None:
            await db.execute(
                "UPDATE handles SET deleted_at = NULL, active = 1, display_name = ?, notes = ? WHERE id = ?",
                (display, note, existing["id"]),
            )
            await db.commit()
            return await _render(
                request, full_page=False, flash=f"Restored handle '{handle}'.", selected_tags=sel
            )
        await db.execute(
            "INSERT INTO handles (linkedin_handle, display_name, notes, active) VALUES (?, ?, ?, 1)",
            (handle, display, note),
        )
        await db.commit()
    return await _render(request, full_page=False, flash=f"Added handle '{handle}'.", selected_tags=sel)


@router.get("/handles/{handle_id}/edit", response_class=HTMLResponse)
async def edit_handle_form(
    request: Request, handle_id: int, tags: Optional[str] = Query(None)
):
    return await _render(
        request,
        full_page=False,
        edit_id=handle_id,
        selected_tags=_parse_tag_param(tags),
    )


@router.post("/handles/{handle_id}/edit", response_class=HTMLResponse)
async def save_handle_edit(
    request: Request,
    handle_id: int,
    display_name: str = Form(""),
    notes: str = Form(""),
    tag_ids: Optional[List[int]] = Form(None),
    selected_tags: str = Form(""),
):
    async with get_db() as db:
        await db.execute(
            "UPDATE handles SET display_name = ?, notes = ? WHERE id = ? AND deleted_at IS NULL",
            (display_name.strip() or None, notes.strip() or None, handle_id),
        )
        await db.execute("DELETE FROM handle_tags WHERE handle_id = ?", (handle_id,))
        ids = tag_ids or []
        if ids:
            await db.executemany(
                "INSERT OR IGNORE INTO handle_tags (handle_id, tag_id) VALUES (?, ?)",
                [(handle_id, tid) for tid in ids],
            )
        await db.commit()
    return await _render(
        request,
        full_page=False,
        flash="Handle updated.",
        selected_tags=_parse_tag_param(selected_tags),
    )


@router.post("/handles/{handle_id}/delete", response_class=HTMLResponse)
async def delete_handle(request: Request, handle_id: int, selected_tags: str = Form("")):
    sel = _parse_tag_param(selected_tags)
    async with get_db() as db:
        cur = await db.execute(
            "SELECT linkedin_handle FROM handles WHERE id = ? AND deleted_at IS NULL",
            (handle_id,),
        )
        row = await cur.fetchone()
        if not row:
            return await _render(request, full_page=False, error="Handle not found.", selected_tags=sel)
        await db.execute(
            "UPDATE handles SET deleted_at = datetime('now'), active = 0 WHERE id = ?",
            (handle_id,),
        )
        await db.commit()
    return await _render(
        request, full_page=False, flash=f"Deleted handle '{row['linkedin_handle']}'.", selected_tags=sel
    )


@router.post("/run-now", response_class=HTMLResponse)
async def run_now(request: Request, selected_tags: str = Form("")):
    sel = _parse_tag_param(selected_tags)
    summary = await run_fetch(trigger="manual")
    if summary.get("skipped"):
        return await _render(request, full_page=False, error=summary["reason"], selected_tags=sel)
    parts = [
        f"{summary['handles_processed']} handles",
        f"{summary['new_posts']} new",
        f"{summary['skipped_duplicates']} duplicates",
        f"{summary.get('comments_generated', 0)} comments",
    ]
    if summary["errors"]:
        parts.append(f"{len(summary['errors'])} errors")
    return await _render(
        request, full_page=False, flash="Fetch complete — " + ", ".join(parts) + ".", selected_tags=sel
    )


@router.post("/auto-tag", response_class=HTMLResponse)
async def auto_tag(request: Request, selected_tags: str = Form("")):
    sel = _parse_tag_param(selected_tags)
    summary = await enrich_untagged_handles()
    if summary.get("skipped"):
        return await _render(request, full_page=False, error=summary["reason"], selected_tags=sel)
    if summary["handles_processed"] == 0:
        return await _render(
            request,
            full_page=False,
            flash="Auto-tag — nothing to do. All handles already have at least one tag.",
            selected_tags=sel,
        )
    msg = (
        f"Auto-tag complete — {summary['handles_processed']} untagged handles, "
        f"{summary['succeeded']} enriched, {summary['failed']} errors."
    )
    failures = [d for d in summary.get("details", []) if d.get("error")]
    if failures:
        first = failures[0]
        msg += f" First error: @{first.get('handle')}: {first.get('error')}"
    return await _render(request, full_page=False, flash=msg, selected_tags=sel)


@router.post("/handles/{handle_id}/toggle", response_class=HTMLResponse)
async def toggle_handle(request: Request, handle_id: int, selected_tags: str = Form("")):
    sel = _parse_tag_param(selected_tags)
    async with get_db() as db:
        await db.execute(
            "UPDATE handles SET active = 1 - active WHERE id = ? AND deleted_at IS NULL",
            (handle_id,),
        )
        await db.commit()
    return await _render(request, full_page=False, selected_tags=sel)


@router.post("/handles/deactivate-all", response_class=HTMLResponse)
async def deactivate_all(request: Request, selected_tags: str = Form("")):
    sel = _parse_tag_param(selected_tags)
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE handles SET active = 0 WHERE active = 1 AND deleted_at IS NULL"
        )
        count = cur.rowcount
        await db.commit()
    msg = (
        f"Deactivated {count} handle(s)."
        if count
        else "Nothing to do — no active handles."
    )
    return await _render(request, full_page=False, flash=msg, selected_tags=sel)


@router.post("/handles/activate-recommended", response_class=HTMLResponse)
async def activate_recommended(
    request: Request,
    handle_ids: str = Form(""),
    selected_tags: str = Form(""),
):
    sel = _parse_tag_param(selected_tags)
    ids = [int(s) for s in handle_ids.split(",") if s.strip().isdigit()]
    if not ids:
        return await _render(
            request,
            full_page=False,
            error="No recommended handles to activate.",
            selected_tags=sel,
        )
    placeholders = ",".join("?" * len(ids))
    async with get_db() as db:
        cur = await db.execute(
            f"UPDATE handles SET active = 1 "
            f"WHERE id IN ({placeholders}) AND active = 0 AND deleted_at IS NULL",
            ids,
        )
        count = cur.rowcount
        await db.commit()
    return await _render(
        request,
        full_page=False,
        flash=f"Activated {count} recommended handle(s).",
        selected_tags=sel,
    )
