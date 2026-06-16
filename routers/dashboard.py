import json
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import tones as tones_store
from comments import generate_for_post, regenerate_one_tone
from database import get_db
from utils import relative_time, truncate

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
templates = Jinja2Templates(directory="templates")

VALID_STATUSES = {"unreviewed", "reviewed", "posted", "dismissed"}
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
    """Pull post image URLs out of the stored Apify response."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    item = data.get("raw") or {}
    imgs = item.get("postImages") or []
    out = []
    for img in imgs:
        if isinstance(img, dict):
            url = img.get("url")
            if isinstance(url, str) and url.startswith("http"):
                out.append(
                    {"url": url, "width": img.get("width"), "height": img.get("height")}
                )
    return out


async def _fetch_status_counts() -> dict:
    async with get_db() as db:
        cur = await db.execute("SELECT status, COUNT(*) AS c FROM posts GROUP BY status")
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
    request: Request, post_id: int, tone_key: str, status: str = Form("unreviewed")
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
    return await _render_dashboard(
        request, full_page=False, status=status, undo_log_id=log_id,
    )


@router.post("/posted/{log_id}/undo", response_class=HTMLResponse)
async def undo_mark_posted(request: Request, log_id: int, status: str = Form("unreviewed")):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT post_id, posted_at FROM posted_log WHERE id = ?", (log_id,)
        )
        row = await cur.fetchone()
    if not row:
        return await _render_dashboard(
            request, full_page=False, status=status,
            error="Already undone or expired.",
        )
    from datetime import datetime, timezone
    try:
        posted_at = datetime.fromisoformat(row["posted_at"].replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        posted_at = datetime.now(timezone.utc)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    age_s = (datetime.now(timezone.utc) - posted_at).total_seconds()
    if age_s > 10:
        return await _render_dashboard(
            request, full_page=False, status=status,
            error="Undo window expired (10 seconds).",
        )
    async with get_db() as db:
        await db.execute("DELETE FROM posted_log WHERE id = ?", (log_id,))
        await db.execute(
            "UPDATE posts SET status = 'reviewed' WHERE id = ?", (row["post_id"],)
        )
        await db.commit()
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
