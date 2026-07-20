"""End-to-end SQL workflow tests against a real file-backed database.

These tests exercise the full pipeline — parser → planner → executor → disk
— without mocks, to catch integration bugs that unit tests miss.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tinydb import Database
from tinydb.errors import ConstraintError, StorageError
from tinydb.executor.planner import plan
from tinydb.executor.scan import IndexScan, SeqScan
from tinydb.parser.parser import parse


@pytest.fixture
def fresh_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "e2e.db"))
    yield db
    db.close()


def _leftmost_scan(db: Database, sql: str):
    """Plan ``sql`` against ``db`` and return the leftmost scan operator.

    End-to-end tests use this to assert whether a query pushed its
    predicate down into an ``IndexScan`` or fell back to a ``SeqScan``,
    mirroring the operator-chain walk the planner produces.
    """
    stmts = parse(sql)
    op = plan(stmts[0], db.catalog, db.pool, db.freelist)
    cur = op
    while hasattr(cur, "child"):
        cur = cur.child
    return cur


def test_create_insert_select_drop_cycle(fresh_db):
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT NOT NULL)")
    fresh_db.execute("INSERT INTO t VALUES (1, 'alice')")
    fresh_db.execute("INSERT INTO t VALUES (2, 'bob')")
    rs = fresh_db.execute("SELECT * FROM t ORDER BY id")
    assert rs.rows == [["1", "alice"], ["2", "bob"]]
    fresh_db.execute("DROP TABLE t")
    with pytest.raises(StorageError):
        fresh_db.execute("SELECT * FROM t")


def test_persistence_across_reopen(tmp_path):
    p = str(tmp_path / "persist.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    for i in range(1, 6):
        db.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")
    db.close()
    db2 = Database(p)
    rs = db2.execute("SELECT SUM(val) FROM t")
    assert rs.rows == [[str(10 + 20 + 30 + 40 + 50)]]
    db2.close()


def test_update_changes_persist(tmp_path):
    p = str(tmp_path / "upd.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, n INT)")
    db.execute("INSERT INTO t VALUES (1, 100)")
    db.execute("INSERT INTO t VALUES (2, 200)")
    db.close()

    db2 = Database(p)
    db2.execute("UPDATE t SET n = n + 1")
    db2.close()

    db3 = Database(p)
    rs = db3.execute("SELECT id, n FROM t ORDER BY id")
    assert rs.rows == [["1", "101"], ["2", "201"]]
    db3.close()


def test_delete_persists(tmp_path):
    p = str(tmp_path / "del.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, n INT)")
    for i in range(1, 6):
        db.execute(f"INSERT INTO t VALUES ({i}, {i})")
    db.close()

    db2 = Database(p)
    db2.execute("DELETE FROM t WHERE n > 2")
    db2.close()

    db3 = Database(p)
    rs = db3.execute("SELECT id FROM t ORDER BY id")
    assert rs.rows == [["1"], ["2"]]
    db3.close()


def test_heap_spans_multiple_pages(tmp_path):
    p = str(tmp_path / "multi.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, data TEXT)")
    for i in range(1, 501):
        # Use longer text to force page overflow.
        db.execute(f"INSERT INTO t VALUES ({i}, 'row {i} padding text')")
    db.close()

    db2 = Database(p)
    rs = db2.execute("SELECT COUNT(*) FROM t")
    assert rs.rows == [["500"]]
    rs = db2.execute("SELECT * FROM t WHERE id = 250")
    assert rs.rows and rs.rows[0][0] == "250"
    db2.close()


def test_unique_index_enforced_across_reopen(tmp_path):
    p = str(tmp_path / "uniq.db")
    db = Database(p)
    db.execute("CREATE TABLE accounts (email TEXT UNIQUE, name TEXT)")
    db.execute("INSERT INTO accounts VALUES ('a@x.com', 'Alice')")
    db.close()

    db2 = Database(p)
    with pytest.raises(ConstraintError):
        db2.execute("INSERT INTO accounts VALUES ('a@x.com', 'Other')")
    db2.close()


def test_index_scan_works_after_reopen(tmp_path):
    p = str(tmp_path / "idx.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    for i in range(1, 11):
        db.execute(f"INSERT INTO t VALUES ({i}, {i * 100})")
    db.close()

    db2 = Database(p)
    rs = db2.execute("SELECT val FROM t WHERE id = 7")
    assert rs.rows == [["700"]]
    db2.close()


def test_group_by_aggregate_persists(tmp_path):
    p = str(tmp_path / "grp.db")
    db = Database(p)
    db.execute("CREATE TABLE orders (region TEXT NOT NULL, amount INT)")
    db.execute("INSERT INTO orders VALUES ('north', 100)")
    db.execute("INSERT INTO orders VALUES ('north', 150)")
    db.execute("INSERT INTO orders VALUES ('south', 200)")
    db.execute("INSERT INTO orders VALUES ('south', 50)")
    db.close()

    db2 = Database(p)
    rs = db2.execute(
        "SELECT region, SUM(amount) FROM orders GROUP BY region ORDER BY region"
    )
    assert rs.rows == [["north", "250"], ["south", "250"]]
    db2.close()


def test_text_with_unicode_and_quotes(tmp_path):
    p = str(tmp_path / "txt.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, msg TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'hello world')")
    db.execute("INSERT INTO t VALUES (2, '你好')")
    db.execute("INSERT INTO t VALUES (3, '')")
    db.close()

    db2 = Database(p)
    rs = db2.execute("SELECT msg FROM t ORDER BY id")
    assert rs.rows == [["hello world"], ["你好"], [""]]
    db2.close()


def test_empty_string_vs_null(tmp_path):
    p = str(tmp_path / "null.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, val TEXT)")
    db.execute("INSERT INTO t VALUES (1, '')")
    db.execute("INSERT INTO t (id) VALUES (2)")  # val defaults to NULL
    db.close()

    db2 = Database(p)
    rs = db2.execute("SELECT id, val FROM t ORDER BY id")
    assert rs.rows == [["1", ""], ["2", ""]]
    db2.close()


def test_multiple_tables_in_one_db(tmp_path):
    p = str(tmp_path / "mt.db")
    db = Database(p)
    db.execute("CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL)")
    db.execute("CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, amount INT)")
    db.execute("INSERT INTO users VALUES (1, 'alice')")
    db.execute("INSERT INTO users VALUES (2, 'bob')")
    db.execute("INSERT INTO orders VALUES (1, 1, 50)")
    db.execute("INSERT INTO orders VALUES (2, 1, 75)")
    db.execute("INSERT INTO orders VALUES (3, 2, 100)")
    db.close()

    db2 = Database(p)
    rs = db2.execute("SELECT user_id, SUM(amount) FROM orders GROUP BY user_id ORDER BY user_id")
    assert rs.rows == [["1", "125"], ["2", "100"]]
    db2.close()


# ---- Index pushdown: inclusive/exclusive bounds end-to-end ---------------
#
# These exercise the full parser → planner → IndexScan → B+ tree path to
# confirm range predicates over an indexed column push a bound down (rather
# than falling back to SeqScan + Filter), that unsafe predicates fall back,
# and that a contradictory range short-circuits to zero rows.


def test_range_predicate_uses_index(fresh_db):
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    for i in range(1, 11):
        fresh_db.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")

    sql = "SELECT id FROM t WHERE id >= 3 AND id <= 6 ORDER BY id"
    assert isinstance(_leftmost_scan(fresh_db, sql), IndexScan)
    rs = fresh_db.execute(sql)
    assert rs.rows == [["3"], ["4"], ["5"], ["6"]]


def test_open_ended_range_uses_index(fresh_db):
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    for i in range(1, 11):
        fresh_db.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")

    sql = "SELECT id FROM t WHERE id > 7 ORDER BY id"
    scan = _leftmost_scan(fresh_db, sql)
    assert isinstance(scan, IndexScan)
    assert scan.bound is not None and not scan.bound.low_inclusive
    rs = fresh_db.execute(sql)
    assert rs.rows == [["8"], ["9"], ["10"]]


def test_contradictory_range_yields_no_rows(fresh_db):
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    for i in range(1, 11):
        fresh_db.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")

    sql = "SELECT id FROM t WHERE id > 8 AND id < 3"
    scan = _leftmost_scan(fresh_db, sql)
    assert isinstance(scan, IndexScan)
    assert scan.bound is not None and scan.bound.always_empty
    rs = fresh_db.execute(sql)
    assert rs.rows == []


def test_or_falls_back_to_seqscan(fresh_db):
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    for i in range(1, 6):
        fresh_db.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")

    sql = "SELECT id FROM t WHERE id = 1 OR id = 4 ORDER BY id"
    assert isinstance(_leftmost_scan(fresh_db, sql), SeqScan)
    rs = fresh_db.execute(sql)
    assert rs.rows == [["1"], ["4"]]


def test_neq_falls_back(fresh_db):
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    for i in range(1, 6):
        fresh_db.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")

    sql = "SELECT id FROM t WHERE id <> 3 ORDER BY id"
    assert isinstance(_leftmost_scan(fresh_db, sql), SeqScan)
    rs = fresh_db.execute(sql)
    assert rs.rows == [["1"], ["2"], ["4"], ["5"]]


def test_range_persists_after_reopen(tmp_db_path):
    db = Database(str(tmp_db_path))
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    for i in range(1, 21):
        db.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")
    db.close()

    db2 = Database(str(tmp_db_path))
    sql = "SELECT id FROM t WHERE id >= 5 AND id <= 8 ORDER BY id"
    assert isinstance(_leftmost_scan(db2, sql), IndexScan)
    rs = db2.execute(sql)
    assert rs.rows == [["5"], ["6"], ["7"], ["8"]]
    db2.close()
