import pytest_asyncio

from database import init_db


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """Fresh, isolated SQLite DB per test. get_db()/db_path() read DATABASE_PATH
    at call time, so pointing the env var at a temp file is enough."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DISCOVERY_QUERIES", "")  # keep the topic seed empty
    await init_db()
    yield
