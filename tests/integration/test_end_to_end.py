"""End-to-end SQL workflow tests against a real file-backed database.

These tests exercise the full pipeline — parser → planner → executor → disk
— without mocks, to catch integration bugs that unit tests miss.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tinydb import Database
from tinydb.errors import ConstraintError, StorageError


@pytest.fixture
def fresh_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "e2e.db"))
    yield db
    db.close()


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
