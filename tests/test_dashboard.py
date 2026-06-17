from database import get_db
from routers.dashboard import _fetch_posts, _fetch_status_counts


async def _insert_post(d, post_id, source, status):
    await d.execute(
        "INSERT INTO posts (post_id, content, source, status, author_handle, author_name) "
        "VALUES (?, 'x', ?, ?, 'a', 'A')",
        (post_id, source, status),
    )


async def test_status_counts_exclude_trending(db):
    # 1 monitored unreviewed + 2 trending unreviewed
    async with get_db() as d:
        await _insert_post(d, "m1", "monitored", "unreviewed")
        await _insert_post(d, "t1", "trending", "unreviewed")
        await _insert_post(d, "t2", "trending", "unreviewed")
        await d.commit()

    counts = await _fetch_status_counts()
    feed = await _fetch_posts("unreviewed")  # defaults to source='monitored'

    # The tab count must match what the feed actually shows.
    assert counts["unreviewed"] == 1
    assert counts["all"] == 1
    assert len(feed) == 1
