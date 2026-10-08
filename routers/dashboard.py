import json
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import tones as tones_store
from comments import generate_for_post, regenerate_one_tone
from database import get_db, unmark_posted
from utils import extract_post_images, relative_time, truncate

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
templates = Jinja2Templates(directory="templates")

VALID_STATUSES = {"unreviewed", "reviewed", "posted", "dismissed"}
# Long enough to come back from LinkedIn after "Copy & comment" and undo there.
UNDO_WINDOW_S = 30 * 60
STATUS_TABS = [
    ("all", "All"),
    ("unreviewed", "Unreviewed"),
    ("reviewed", "Reviewed"),
    ("posted", "Posted"),
    ("dismissed", "Dismissed"),
]


def _parse_engagement(raw: Optional[str]) -> dict:
    if not raw:
        return {"reactions": None, "comments": None, "reposts": None}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"reactions": None, "comments": None, "reposts": None}
    return {
        "reactions": data.get("reactions"),
        "comments": data.get("comments"),
        "reposts": data.get("reposts"),
    }


def _parse_images(raw: Optional[str]) -> list[dict]:
    """Pull post image URLs out of the stored engagement blob. Older rows keep
    the full Apify item under "raw"; newer and pruned rows store "images"."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if "images" in data:
        return data["images"] or []
    return extract_post_images(data.get("raw") or {})


async def _fetch_status_counts() -> dict:
    # Scoped to monitored posts so the tab counts match the dashboard feed,
    # which excludes trending posts (those live on /discover).
    async with get_db() as db:
        cur = await db.execute(
            "SELECT status, COUNT(*) AS c FROM posts "
            "WHERE source = 'monitored' OR source IS NULL GROUP BY status"
        )
        rows = await cur.fetchall()
    counts = {s: 0 for s in VALID_STATUSES}
    counts["all"] = 0
    for r in rows:
        if r["status"] in counts:
            counts[r["status"]] = r["c"]
        counts["all"] += r["c"]
    return counts


async def _fetch_posted_tones(post_ids: list[int]) -> dict[int, str]:
    """Latest posted tone per post_id (one row per post by design)."""
    if not post_ids:
        return {}
    qmarks = ",".join("?" for _ in post_ids)
    async with get_db() as db:
        cur = await db.execute(
            f"SELECT post_id, tone FROM posted_log WHERE post_id IN ({qmarks})",
            post_ids,
        )
        rows = await cur.fetchall()
    return {r["post_id"]: r["tone"] for r in rows}


async def _fetch_posts(
    status: str, source: str = "monitored", order: str = "recent"
) -> list[dict]:
    # Trending posts share the posts table; the source filter keeps the monitored
    # dashboard and the discovery view from intermixing. Treat legacy NULL as monitored.
    clauses, params = [], []
    if source == "monitored":
        clauses.append("(p.source = 'monitored' OR p.source IS NULL)")
    else:
        clauses.append("p.source = ?")
        params.append(source)
    if status != "all":
        clauses.append("p.status = ?")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses)
    order_sql = (
        "ORDER BY p.engagement_score DESC, p.id DESC"
        if order == "engagement"
        else "ORDER BY COALESCE(p.posted_at, p.fetched_at) DESC, p.id DESC"
    )
    async with get_db() as db:
        cur = await db.execute(
            f"""
            SELECT p.id, p.post_id, p.content, p.summary, p.url, p.engagement_json,
                   p.posted_at, p.fetched_at, p.status, p.engagement_score, p.source,
                   COALESCE(h.linkedin_handle, p.author_handle) AS linkedin_handle,
                   COALESCE(h.display_name, p.author_name) AS display_name,
                   h.deleted_at AS handle_deleted_at,
                   p.handle_id
            FROM posts p LEFT JOIN handles h ON p.handle_id = h.id
            {where}
            {order_sql}
            """,
            params,
        )
        post_rows = await cur.fetchall()

        if not post_rows:
            return []

        post_ids = [r["id"] for r in post_rows]
        qmarks = ",".join("?" for _ in post_ids)
        cur = await db.execute(
            f"SELECT id, post_id, tone, content, edited FROM generated_comments "
            f"WHERE post_id IN ({qmarks})",
            post_ids,
        )
        comment_rows = await cur.fetchall()

    by_post: dict[int, dict[str, dict]] = {pid: {} for pid in post_ids}
    for c in comment_rows:
        by_post[c["post_id"]][c["tone"]] = {
            "id": c["id"],
            "content": c["content"],
            "edited": bool(c["edited"]),
        }

    posted_tones = await _fetch_posted_tones(post_ids)
    tones = tones_store.get_all()
    tone_names = {t["key"]: t["name"] for t in tones}

    posts = []
    for r in post_rows:
        engagement = _parse_engagement(r["engagement_json"])
        images = _parse_images(r["engagement_json"])
        time_iso = r["posted_at"] or r["fetched_at"]
        posted_tone = posted_tones.get(r["id"])
        # Render the 6 tones in canonical order; mark missing ones explicitly.
        tone_blocks = []
        for t in tones:
            c = by_post[r["id"]].get(t["key"])
            tone_blocks.append(
                {
                    "key": t["key"],
                    "name": t["name"],
                    "description": t.get("description") or "",
                    "comment": c,
                    "is_posted": (t["key"] == posted_tone),
                }
            )
        posts.append(
            {
                "id": r["id"],
                "handle": r["linkedin_handle"],
                "display_name": r["display_name"] or r["linkedin_handle"],
                "handle_deleted": r["handle_deleted_at"] is not None,
                "preview": truncate(r["content"], 300),
                "full_content": r["content"],
                "summary": r["summary"],
                "url": r["url"],
                "engagement": engagement,
                "images": images,
                "time_iso": time_iso,
                "time_display": relative_time(time_iso),
                "status": r["status"],
                "tone_blocks": tone_blocks,
                "comment_count": sum(1 for b in tone_blocks if b["comment"]),
                "can_regenerate": r["status"] != "posted",
                "is_posted_status": r["status"] == "posted",
                "already_posted": posted_tone is not None,
                "posted_tone_name": tone_names.get(posted_tone) if posted_tone else None,
                "engagement_score": r["engagement_score"],
                "author_monitored": r["handle_id"] is not None,
                "is_trending": r["source"] == "trending",
            }
        )
    return posts


async def _render_dashboard(
    request: Request,
    *,
    full_page: bool,
    status: str = "unreviewed",
    flash: Optional[str] = None,
    error: Optional[str] = None,
    undo_log_id: Optional[int] = None,
    undo_detail: Optional[str] = None,
):
    if status not in {"all", *VALID_STATUSES}:
        status = "unreviewed"
    posts = await _fetch_posts(status)
    counts = await _fetch_status_counts()
    ctx = {
        "posts": posts,
        "active_status": status,
        "counts": counts,
        "status_tabs": STATUS_TABS,
        "flash": flash,
        "error": error,
        "undo_log_id": undo_log_id,
        "undo_detail": undo_detail,
    }
    template = "dashboard.html" if full_page else "_dashboard_main.html"
    return templates.TemplateResponse(request, template, ctx)


async def _fetch_single_comment(post_id: int, tone_key: str) -> Optional[dict]:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, content, edited FROM generated_comments "
            "WHERE post_id = ? AND tone = ?",
            (post_id, tone_key),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {"id": row["id"], "content": row["content"], "edited": bool(row["edited"])}


def _tone_meta(tone_key: str) -> dict:
    t = tones_store.get(tone_key)
    if not t:
        return {"key": tone_key, "name": tone_key, "description": ""}
    return {"key": t["key"], "name": t["name"], "description": t.get("description") or ""}


async def _post_context(post_id: int) -> dict:
    """What a standalone comment block needs to render its post actions."""
    async with get_db() as db:
        cur = await db.execute(
            "SELECT p.url, p.source, p.status, "
            "(SELECT tone FROM posted_log WHERE post_id = p.id) AS posted_tone "
            "FROM posts p WHERE p.id = ?",
            (post_id,),
        )
        row = await cur.fetchone()
    if not row:
        return {"url": None, "base": "/dashboard", "is_posted_status": False, "posted_tone": None}
    return {
        "url": row["url"],
        # Trending posts are only shown on /discover; route actions back there.
        "base": "/discover" if row["source"] == "trending" else "/dashboard",
        "is_posted_status": row["status"] == "posted",
        "posted_tone": row["posted_tone"],
    }


async def _posted_detail(post_id: int, tone_key: str) -> str:
    """Undo-banner text naming what was just marked posted."""
    async with get_db() as db:
        cur = await db.execute(
            "SELECT COALESCE(h.display_name, p.author_name, h.linkedin_handle, "
            "p.author_handle) AS name "
            "FROM posts p LEFT JOIN handles h ON p.handle_id = h.id WHERE p.id = ?",
            (post_id,),
        )
        row = await cur.fetchone()
    # Display names often carry a headline ("Jane Doe — CEO at X"); keep the name.
    name = ((row["name"] if row else None) or "this").split(" — ")[0].split(" - ")[0]
    return f"Marked your {_tone_meta(tone_key)['name']} comment on {name}'s post as posted."


async def _render_comment_block(
    request: Request,
    post_id: int,
    tone_key: str,
    editing: bool = False,
    can_regenerate: bool = True,
):
    comment = await _fetch_single_comment(post_id, tone_key)
    return templates.TemplateResponse(
        request,
        "_comment_block.html",
        {
            "post_id": post_id,
            "tone": _tone_meta(tone_key),
            "comment": comment,
            "editing": editing,
            "can_regenerate": can_regenerate,
            "post": await _post_context(post_id),
        },
    )


async def _post_status(post_id: int) -> Optional[str]:
    async with get_db() as db:
        cur = await db.execute("SELECT status FROM posts WHERE id = ?", (post_id,))
        row = await cur.fetchone()
    return row["status"] if row else None


async def _set_status_if(post_id: int, new_status: str, only_from: Optional[set] = None) -> None:
    async with get_db() as db:
        if only_from:
            qmarks = ",".join("?" for _ in only_from)
            await db.execute(
                f"UPDATE posts SET status = ? WHERE id = ? AND status IN ({qmarks})",
                (new_status, post_id, *only_from),
            )
        else:
            await db.execute(
                "UPDATE posts SET status = ? WHERE id = ?", (new_status, post_id)
            )
        await db.commit()


# ----- Routes -----

@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, status: str = "unreviewed"):
    is_htmx = request.headers.get("hx-request") == "true"
    return await _render_dashboard(request, full_page=not is_htmx, status=status)


@router.post("/posts/{post_id}/dismiss", response_class=HTMLResponse)
async def dismiss_post(request: Request, post_id: int, status: str = Form("unreviewed")):
    await _set_status_if(post_id, "dismissed")
    return await _render_dashboard(request, full_page=False, status=status, flash="Post dismissed.")


@router.post("/posts/{post_id}/mark-reviewed", response_class=HTMLResponse)
async def mark_reviewed(post_id: int):
    # Only escalate from 'unreviewed' to 'reviewed'; never downgrade or alter posted/dismissed.
    await _set_status_if(post_id, "reviewed", only_from={"unreviewed"})
    return HTMLResponse("", status_code=204)


@router.post("/posts/{post_id}/regenerate", response_class=HTMLResponse)
async def regenerate_all(request: Request, post_id: int, status: str = Form("unreviewed")):
    s = await _post_status(post_id)
    if s == "posted":
        return await _render_dashboard(
            request, full_page=False, status=status,
            error="Cannot regenerate a posted post.",
        )
    try:
        summary = await generate_for_post(post_id)
    except Exception as e:
        return await _render_dashboard(
            request, full_page=False, status=status,
            error=f"Regeneration failed: {e}",
        )
    msg = (
        f"Regenerated post {post_id}: "
        f"{summary['generated']} new, {summary['skipped']} skipped"
        + (f", {len(summary['errors'])} errors" if summary["errors"] else "")
    )
    return await _render_dashboard(request, full_page=False, status=status, flash=msg)


@router.get("/posts/{post_id}/comments/{tone_key}", response_class=HTMLResponse)
async def comment_read(request: Request, post_id: int, tone_key: str):
    s = await _post_status(post_id)
    return await _render_comment_block(
        request, post_id, tone_key, editing=False, can_regenerate=(s != "posted")
    )


@router.get("/posts/{post_id}/comments/{tone_key}/edit", response_class=HTMLResponse)
async def comment_edit_form(request: Request, post_id: int, tone_key: str):
    s = await _post_status(post_id)
    return await _render_comment_block(
        request, post_id, tone_key, editing=True, can_regenerate=(s != "posted")
    )


@router.post("/posts/{post_id}/comments/{tone_key}", response_class=HTMLResponse)
async def comment_save(
    request: Request, post_id: int, tone_key: str, content: str = Form(...)
):
    trimmed = content.strip()
    if not trimmed:
        raise HTTPException(400, "Comment cannot be empty.")
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id FROM generated_comments WHERE post_id = ? AND tone = ?",
            (post_id, tone_key),
        )
        existing = await cur.fetchone()
        if existing:
            await db.execute(
                "UPDATE generated_comments SET content = ?, edited = 1 WHERE id = ?",
                (trimmed, existing["id"]),
            )
        else:
            await db.execute(
                "INSERT INTO generated_comments (post_id, tone, content, edited) VALUES (?, ?, ?, 1)",
                (post_id, tone_key, trimmed),
            )
        await db.commit()
    await _set_status_if(post_id, "reviewed", only_from={"unreviewed"})
    s = await _post_status(post_id)
    return await _render_comment_block(
        request, post_id, tone_key, editing=False, can_regenerate=(s != "posted")
    )


@router.post("/posts/{post_id}/comments/{tone_key}/mark-posted", response_class=HTMLResponse)
async def mark_posted(
    request: Request,
    post_id: int,
    tone_key: str,
    status: str = Form("unreviewed"),
    via: str = Form(""),
):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, content FROM generated_comments WHERE post_id = ? AND tone = ?",
            (post_id, tone_key),
        )
        comment = await cur.fetchone()
        if not comment:
            return await _render_dashboard(
                request, full_page=False, status=status,
                error=f"No comment found for tone '{tone_key}' on this post.",
            )
        # One posted-log row per post; replace any prior selection.
        await db.execute("DELETE FROM posted_log WHERE post_id = ?", (post_id,))
        cur = await db.execute(
            "INSERT INTO posted_log (post_id, comment_id, tone) VALUES (?, ?, ?)",
            (post_id, comment["id"], tone_key),
        )
        log_id = cur.lastrowid
        await db.execute("UPDATE posts SET status = 'posted' WHERE id = ?", (post_id,))
        await db.commit()
    # "Copy & comment" sends you off to LinkedIn, so its undo banner names
    # what was marked and stays up rather than counting down.
    detail = await _posted_detail(post_id, tone_key) if via == "copy-comment" else None
    return await _render_dashboard(
        request, full_page=False, status=status, undo_log_id=log_id, undo_detail=detail,
    )


@router.post("/posted/{log_id}/undo", response_class=HTMLResponse)
async def undo_mark_posted(request: Request, log_id: int, status: str = Form("unreviewed")):
    err = await unmark_posted(log_id, max_age_s=UNDO_WINDOW_S)
    if err:
        return await _render_dashboard(request, full_page=False, status=status, error=err)
    return await _render_dashboard(
        request, full_page=False, status=status, flash="Undone."
    )


@router.post("/posts/{post_id}/comments/{tone_key}/regenerate", response_class=HTMLResponse)
async def comment_regenerate(request: Request, post_id: int, tone_key: str):
    s = await _post_status(post_id)
    if s == "posted":
        raise HTTPException(409, "Cannot regenerate a posted post.")
    try:
        await regenerate_one_tone(post_id, tone_key)
    except Exception as e:
        # Render the slot with an error flash inside it.
        return templates.TemplateResponse(
            request,
            "_comment_block.html",
            {
                "post_id": post_id,
                "tone": _tone_meta(tone_key),
                "comment": await _fetch_single_comment(post_id, tone_key),
                "editing": False,
                "can_regenerate": True,
                "regenerate_error": str(e),
            },
        )
    return await _render_comment_block(
        request, post_id, tone_key, editing=False, can_regenerate=True
    )
