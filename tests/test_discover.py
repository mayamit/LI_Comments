import pytest

import discover
from database import get_db


# --------------------------- pure functions (no DB) ---------------------------


def test_score_engagement_weights_comments_and_reposts():
    item = {"engagement": {"likes": 10, "comments": 5, "shares": 2}}
    # reactions + 2*comments + 3*reposts = 10 + 10 + 6
    assert discover.score_engagement(item) == 26


def test_score_engagement_empty_is_zero():
    assert discover.score_engagement({}) == 0


def test_extract_engagement_falls_back_to_flat_fields():
    item = {"reactionsCount": 7, "commentsCount": 3, "repostsCount": 1}
    e = discover._extract_engagement(item)
    assert (e["reactions"], e["comments"], e["reposts"]) == (7, 3, 1)


def test_extract_author_combines_name_and_headline():
    a = discover._extract_author(
        {"author": {"name": "Jane Doe", "publicIdentifier": "janedoe", "info": "CEO at X"}}
    )
    assert a["handle"] == "janedoe"
    assert a["name"] == "Jane Doe — CEO at X"


def test_extract_author_handles_missing_fields():
    a = discover._extract_author({})
    assert a == {"handle": None, "name": None}


# --------------------------- topic CRUD (DB) ---------------------------


async def test_topic_crud_lifecycle(db):
    await discover.add_topic("AI agents")
    await discover.add_topic("AI agents")  # duplicate ignored (UNIQUE)
    topics = await discover.get_all_topics()
    assert len(topics) == 1
    assert await discover.get_active_topics() == ["AI agents"]

    await discover.toggle_topic(topics[0]["id"])
    assert await discover.get_active_topics() == []  # inactive hidden from runs
    assert len(await discover.get_all_topics()) == 1  # still listed for management

    await discover.delete_topic(topics[0]["id"])
    assert await discover.get_all_topics() == []


async def test_add_topic_rejects_blank(db):
    with pytest.raises(discover.DiscoveryError):
        await discover.add_topic("   ")


# --------------------------- promotion (DB) ---------------------------


async def _insert_trending(db_conn, post_id, author_handle, handle_id=None):
    await db_conn.execute(
        "INSERT INTO posts (post_id, content, source, author_handle, author_name, "
        "engagement_score, handle_id) VALUES (?, 'x', 'trending', ?, 'Jane Doe', 5, ?)",
        (post_id, author_handle, handle_id),
    )


async def test_promote_author_creates_handle_and_backfills_siblings(db):
    async with get_db() as d:
        await _insert_trending(d, "p1", "janedoe")
        await _insert_trending(d, "p2", "janedoe")  # sibling, same author
        await d.commit()
        row = await (await d.execute("SELECT id FROM posts WHERE post_id='p1'")).fetchone()
        post_id = row[0]

    result = await discover.promote_author(post_id)
    assert result["created"] is True

    async with get_db() as d:
        h = await (
            await d.execute("SELECT id, active FROM handles WHERE linkedin_handle='janedoe'")
        ).fetchone()
        assert h is not None and h[1] == 1
        cnt = await (
            await d.execute("SELECT COUNT(*) FROM posts WHERE handle_id = ?", (h[0],))
        ).fetchone()
        assert cnt[0] == 2  # both siblings linked


async def test_promote_already_monitored_raises(db):
    async with get_db() as d:
        await d.execute("INSERT INTO handles (linkedin_handle, active) VALUES ('bob', 1)")
        hrow = await (await d.execute("SELECT id FROM handles WHERE linkedin_handle='bob'")).fetchone()
        await _insert_trending(d, "pb", "bob", handle_id=hrow[0])
        await d.commit()
        prow = await (await d.execute("SELECT id FROM posts WHERE post_id='pb'")).fetchone()

    with pytest.raises(discover.DiscoveryError):
        await discover.promote_author(prow[0])
