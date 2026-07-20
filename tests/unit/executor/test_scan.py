"""Tests for SeqScan and IndexScan."""

from __future__ import annotations

import pytest

from tinydb import Database
from tinydb.executor.planner import IndexBound
from tinydb.executor.scan import IndexScan
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


# ---- IndexBound honored by IndexScan -------------------------------------
#
# Task 3: IndexScan.open() must consult self.bound and route through
# range_scan_with_bound so the inclusive/exclusive semantics push down
# into the B+ tree. These tests exercise IndexScan directly through
# the planner using a real Database fixture.


def _index_scan_for_pk(seeded, where_sql: str) -> IndexScan:
    """Plan a SELECT on the seeded PK table and return the IndexScan.

    The seeded fixture installs 5 rows on a PK column ``id``; any
    equality / range predicate over ``id`` plans down to an IndexScan
    via the planner's ``_extract_index_bound``.
    """
    from tinydb.executor.planner import plan
    from tinydb.parser.parser import parse

    stmts = parse(f"SELECT * FROM t WHERE {where_sql}")
    op = plan(stmts[0], seeded.catalog, seeded.pool, seeded.freelist)
    cur = op
    while hasattr(cur, "child"):
        cur = cur.child
    assert isinstance(cur, IndexScan), f"expected IndexScan, got {type(cur).__name__}"
    return cur  # type: ignore[return-value]


def _drain(scan: IndexScan) -> list:
    rows = []
    scan.open()
    while True:
        r = scan.next()
        if r is None:
            break
        rows.append(r)
    scan.close()
    return rows


def test_index_scan_with_eq_bound(seeded):
    scan = _index_scan_for_pk(seeded, "id = 3")
    assert scan.bound is not None and scan.bound.column == "id"
    rows = _drain(scan)
    assert len(rows) == 1
    row = rows[0]
    assert row.values["id"] == Value.int_(3)
    assert row.values["name"] == Value.text("carol")
    assert row.values["score"] == Value.int_(90)


def test_index_scan_with_range_bound(seeded):
    scan = _index_scan_for_pk(seeded, "id >= 2 AND id <= 4")
    assert scan.bound is not None
    assert scan.bound.low_inclusive is True
    assert scan.bound.high_inclusive is True
    rows = _drain(scan)
    # Bound is inclusive on both ends, B+ tree returns ascending order.
    assert [r.values["id"].payload for r in rows] == [2, 3, 4]
    assert rows[0].values["name"] == Value.text("bob")
    assert rows[1].values["name"] == Value.text("carol")
    assert rows[2].values["name"] == Value.text("dave")


def test_index_scan_with_contradictory_bound_returns_empty(seeded):
    scan = _index_scan_for_pk(seeded, "id > 10 AND id < 5")
    assert scan.bound is not None
    assert scan.bound.always_empty is True
    rows = _drain(scan)
    assert rows == []


def test_index_scan_without_bound_remains_full(seeded):
    scan = _index_scan_for_pk(seeded, "id = 1")
    # Strip the bound to mimic a planner that decided not to push one
    # down; IndexScan.open() must keep the legacy full-range behavior.
    scan.bound = None
    rows = _drain(scan)
    assert len(rows) == 5
    assert [r.values["id"].payload for r in rows] == [1, 2, 3, 4, 5]  # full PK order
