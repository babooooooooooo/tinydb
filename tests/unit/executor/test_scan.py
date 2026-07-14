"""Tests for SeqScan and IndexScan."""

from __future__ import annotations

import pytest

from tinydb import Database
from tinydb.types import Tag, Value


@pytest.fixture
def seeded(tmp_db):
    """Create a small table with a handful of rows for scanning."""
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT NOT NULL, score INT)")
    rows = [
        (1, "alice", 80),
        (2, "bob", 60),
        (3, "carol", 90),
        (4, "dave", 70),
        (5, "eve", 50),
    ]
    for r in rows:
        tmp_db.execute(
            f"INSERT INTO t VALUES ({r[0]}, '{r[1]}', {r[2]})"
        )
    return tmp_db


def test_seq_scan_returns_all_rows(seeded):
    rs = seeded.execute("SELECT * FROM t")
    assert len(rs.rows) == 5
    assert rs.columns == ["id", "name", "score"]


def test_seq_scan_order_matches_insertion(seeded):
    rs = seeded.execute("SELECT * FROM t")
    ids = [r[0] for r in rs.rows]
    assert ids == ["1", "2", "3", "4", "5"]


def test_index_scan_picks_specific_row(seeded):
    rs = seeded.execute("SELECT * FROM t WHERE id = 3")
    assert rs.rows == [["3", "carol", "90"]]


def test_index_scan_returns_empty_when_no_match(seeded):
    rs = seeded.execute("SELECT * FROM t WHERE id = 999")
    assert rs.rows == []


def test_index_scan_via_conjunction(seeded):
    rs = seeded.execute("SELECT * FROM t WHERE id = 2 AND score = 60")
    assert rs.rows == [["2", "bob", "60"]]


def test_scan_with_no_index_falls_back_to_seq(seeded):
    rs = seeded.execute("SELECT * FROM t WHERE score > 70")
    assert len(rs.rows) == 2  # alice and carol
