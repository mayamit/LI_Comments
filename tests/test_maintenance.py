import json
import sqlite3

from database import get_db
from maintenance import archive_path, run_maintenance
from routers.dashboard import _parse_engagement, _parse_images

RAW = json.dumps(
    {
        "reactions": 5,
        "comments": 2,
        "reposts": 1,
        "raw": {
            "content": "x" * 500,
            "postImages": [{"url": "https://img/1.jpg", "width": 10, "height": 20}],
        },
    }
)


async def _post(d, post_id, status, age_days):
    cur = await d.execute(
        "INSERT INTO posts (post_id, content, status, engagement_json, fetched_at) "
        "VALUES (?, 'x', ?, ?, datetime('now', ?))",
        (post_id, status, RAW, f"-{age_days} days"),
    )
    return cur.lastrowid


async def _comment(d, post_id, tone):
    cur = await d.execute(
        "INSERT INTO generated_comments (post_id, tone, content) VALUES (?, ?, 'c')",
        (post_id, tone),
    )
    return cur.lastrowid


async def _seed():
    async with get_db() as d:
        old_posted = await _post(d, "old-posted", "posted", 30)
        old_unrev = await _post(d, "old-unrev", "unreviewed", 30)
        new_unrev = await _post(d, "new-unrev", "unreviewed", 1)
        chosen = await _comment(d, old_posted, "curious")
        await _comment(d, old_posted, "wry")
        await _comment(d, old_unrev, "wry")
        await _comment(d, new_unrev, "wry")
        await d.execute(
            "INSERT INTO posted_log (post_id, comment_id, tone) VALUES (?, ?, 'curious')",
            (old_posted, chosen),
        )
        await d.execute(
            "UPDATE handles SET enrichment_json = '{\"big\": 1}'"
        )
        await d.commit()
    return old_posted, old_unrev, new_unrev, chosen


async def _rows(sql, params=()):
    async with get_db() as d:
        cur = await d.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]


async def test_prunes_old_posts_and_keeps_recent(db):
    old_posted, old_unrev, new_unrev, chosen = await _seed()

    s = await run_maintenance()
    assert (s["dismissed"], s["comments_dropped"], s["posts_slimmed"]) == (1, 2, 2)

    status = {r["post_id"]: r["status"] for r in await _rows("SELECT post_id, status FROM posts")}
    assert status == {"old-posted": "posted", "old-unrev": "dismissed", "new-unrev": "unreviewed"}

    # Only the posted comment survives on old posts; recent posts are untouched.
    comments = await _rows("SELECT id, post_id FROM generated_comments ORDER BY id")
    assert [c["id"] for c in comments if c["post_id"] != new_unrev] == [chosen]
    assert any(c["post_id"] == new_unrev for c in comments)

    # Old blobs are slimmed but still render; the recent one keeps its raw item.
    blobs = {r["id"]: r["engagement_json"] for r in await _rows("SELECT id, engagement_json FROM posts")}
    assert '"raw"' not in blobs[old_posted]
    assert _parse_engagement(blobs[old_posted])["reactions"] == 5
    assert _parse_images(blobs[old_posted]) == [{"url": "https://img/1.jpg", "width": 10, "height": 20}]
    assert '"raw"' in blobs[new_unrev]

    assert (await _rows("SELECT COUNT(*) n FROM handles WHERE enrichment_json IS NOT NULL"))[0]["n"] == 0

    # Everything removed from the main DB is in the archive, raw blobs intact.
    a = sqlite3.connect(archive_path())
    assert a.execute("SELECT COUNT(*) FROM generated_comments").fetchone()[0] == 2
    archived = a.execute("SELECT engagement_json FROM posts WHERE id = ?", (old_posted,)).fetchone()[0]
    assert '"raw"' in archived
    assert a.execute("SELECT COUNT(*) FROM handle_profiles").fetchone()[0] >= 1
    a.close()

    # Rerunning finds nothing more to do.
    s = await run_maintenance()
    assert (s["dismissed"], s["comments_dropped"], s["posts_slimmed"], s["profiles_cleared"]) == (0, 0, 0, 0)


async def test_dry_run_changes_nothing(db):
    await _seed()
    s = await run_maintenance(dry_run=True)
    assert (s["dismissed"], s["comments_dropped"], s["posts_slimmed"]) == (1, 2, 2)
    assert (await _rows("SELECT COUNT(*) n FROM generated_comments"))[0]["n"] == 4
    assert (await _rows("SELECT COUNT(*) n FROM posts WHERE status = 'unreviewed'"))[0]["n"] == 2
