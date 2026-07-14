"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Return a unique temp file path for a tinydb database.

    The file is NOT created; the test (or fixture consumer) decides whether
    to open with `Database.create=True` or expect an existing file.
    """
    return tmp_path / "test.db"


@pytest.fixture
def tmp_db(tmp_db_path: Path):
    """Yield a freshly-opened Database at a temp path, closed on teardown.

    Usage:
        def test_something(tmp_db):
            tmp_db.execute("CREATE TABLE ...")
    """
    from tinydb import Database

    db = Database(str(tmp_db_path))
    try:
        yield db
    finally:
        db.close()