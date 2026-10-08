import httpx
import pytest_asyncio
from fastapi import FastAPI

from database import get_db
from routers import dashboard, discover, history

URL = "https://www.linkedin.com/feed/update/urn:li:activity:1"


@pytest_asyncio.fixture
async def client(db):
    app = FastAPI()
    for r in (dashboard, discover, history):
        app.include_router(r.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _post(post_id, url=URL, source="monitored", status="unreviewed"):
    async with get_db() as d:
        cur = await d.execute(
            "INSERT INTO posts (handle_id, post_id, content, url, source, status, "
            "author_handle, author_name) VALUES (1, ?, 'post', ?, ?, ?, 'a', 'Jane Doe')",
            (post_id, url, source, status),
        )
        pid = cur.lastrowid
        for tone in ("curious", "wry"):
            await d.execute(
                "INSERT INTO generated_comments (post_id, tone, content) VALUES (?, ?, ?)",
                (pid, tone, f"{tone} comment"),
            )
        await d.commit()
    return pid


async def _status(pid):
    async with get_db() as d:
        cur = await d.execute("SELECT status FROM posts WHERE id = ?", (pid,))
        return (await cur.fetchone())["status"]


async def _log_count():
    async with get_db() as d:
        cur = await d.execute("SELECT COUNT(*) FROM posted_log")
        return (await cur.fetchone())[0]


async def test_button_rendered_per_tone_with_post_url(client):
    pid = await _post("p1")
    await _post("no-url", url=None)
    html = (await client.get("/dashboard")).text
    assert html.count("Copy &amp; comment ↗") == 2  # one per tone, none for the url-less post
    assert f'data-post-url="{URL}"' in html
    assert f'data-mark-url="/dashboard/posts/{pid}/comments/curious/mark-posted"' in html
    assert "Open on LinkedIn ↗" in html  # header link kept as the fallback


async def test_trending_button_routes_to_discover(client):
    pid = await _post("t1", source="trending")
    html = (await client.get("/discover")).text
    assert f'data-mark-url="/discover/posts/{pid}/comments/curious/mark-posted"' in html


async def test_comment_block_after_edit_keeps_post_actions(client):
    pid = await _post("p1")
    html = (await client.get(f"/dashboard/posts/{pid}/comments/curious")).text
    assert "Copy &amp; comment ↗" in html
    assert "Mark posted" in html


async def test_copy_comment_marks_posted_with_sticky_undo(client):
    pid = await _post("p1")
    r = await client.post(
        f"/dashboard/posts/{pid}/comments/curious/mark-posted",
        data={"status": "unreviewed", "via": "copy-comment"},
    )
    # Handle 1 is the seeded owner; the headline after " - " is trimmed.
    assert "Marked your Curious comment on Amit Gandhi&#39;s post as posted." in r.text
    assert "undo-banner-sticky" in r.text and "undo-timer" not in r.text
    assert await _status(pid) == "posted"
    assert await _log_count() == 1

    # Plain "Mark posted" keeps the 10-second countdown banner.
    r = await client.post(f"/dashboard/posts/{pid}/comments/wry/mark-posted", data={"status": "all"})
    assert "undo-timer" in r.text


async def test_already_posted_tone_only_opens_and_copies(client):
    pid = await _post("p1")
    await client.post(f"/dashboard/posts/{pid}/comments/curious/mark-posted", data={"status": "all"})
    html = (await client.get(f"/dashboard/posts/{pid}/comments/curious")).text
    assert "Copy &amp; comment ↗" in html and "data-mark-url" not in html
    # Another tone on the same post asks before replacing the posted one.
    html = (await client.get(f"/dashboard/posts/{pid}/comments/wry")).text
    assert 'data-confirm="Replace the previously-posted comment with this one?"' in html


async def test_undo_window_is_30_minutes(client):
    pid = await _post("p1")
    await client.post(f"/dashboard/posts/{pid}/comments/curious/mark-posted", data={"via": "copy-comment"})
    async with get_db() as d:
        await d.execute("UPDATE posted_log SET posted_at = datetime('now', '-29 minutes')")
        await d.commit()
    r = await client.post("/dashboard/posted/1/undo", data={"status": "all"})
    assert "Undone." in r.text
    assert await _status(pid) == "reviewed" and await _log_count() == 0

    await client.post(f"/dashboard/posts/{pid}/comments/curious/mark-posted", data={"via": "copy-comment"})
    async with get_db() as d:
        await d.execute("UPDATE posted_log SET posted_at = datetime('now', '-31 minutes')")
        await d.commit()
    r = await client.post("/dashboard/posted/2/undo", data={"status": "all"})
    assert "Undo window expired (30 minutes)." in r.text
    assert await _log_count() == 1


async def test_history_unmark_any_age(client):
    async with get_db() as d:
        await d.execute("INSERT INTO handles (linkedin_handle) VALUES ('jane')")
        await d.commit()
    pid = await _post("p1")
    await client.post(f"/dashboard/posts/{pid}/comments/curious/mark-posted", data={"status": "all"})
    async with get_db() as d:
        await d.execute("UPDATE posted_log SET posted_at = datetime('now', '-10 days')")
        await d.commit()
    assert "Unmark posted" in (await client.get("/history")).text

    r = await client.post("/history/1/unmark")
    assert r.status_code == 204 and r.headers["HX-Refresh"] == "true"
    assert await _status(pid) == "reviewed" and await _log_count() == 0
    # The comment itself is kept, so the post is back on the dashboard intact.
    html = (await client.get("/dashboard?status=reviewed")).text
    assert "curious comment" in html
