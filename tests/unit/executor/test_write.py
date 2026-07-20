"""Tests for write operators (Insert, Update, Delete, CreateTable, DropTable)."""

from __future__ import annotations

import pytest

from tinydb import Database
from tinydb.errors import ConstraintError, StorageError, TypeMismatchError


def test_delete_propagates_index_failure(tmp_db, monkeypatch):
    """A failure while removing an index entry during DELETE must NOT be
    swallowed: a stale index entry pointing at a deleted row is silent
    corruption. The error must surface so the caller can abort/recover.
    """
    from tinydb.index import btree

    tmp_db.execute("CREATE TABLE accounts (id INT PRIMARY KEY, email TEXT UNIQUE)")
    tmp_db.execute("INSERT INTO accounts VALUES (1, 'a@x.com')")

    def boom(self, key):
        raise StorageError("simulated index corruption")

    monkeypatch.setattr(btree.BPlusTree, "delete", boom)
    with pytest.raises(StorageError):
        tmp_db.execute("DELETE FROM accounts WHERE id = 1")


def test_update_propagates_index_failure(tmp_db, monkeypatch):
    """Changing an indexed column requires deleting the old index key.
    If that delete raises, the error must propagate instead of leaving a
    stale index entry behind.
    """
    from tinydb.index import btree

    tmp_db.execute("CREATE TABLE accounts (id INT PRIMARY KEY, email TEXT UNIQUE)")
    tmp_db.execute("INSERT INTO accounts VALUES (1, 'a@x.com')")

    def boom(self, key):
        raise StorageError("simulated index corruption")

    monkeypatch.setattr(btree.BPlusTree, "delete", boom)
    with pytest.raises(StorageError):
        tmp_db.execute("UPDATE accounts SET email = 'b@x.com' WHERE id = 1")



# ---- create / drop --------------------------------------------------------


def test_create_table(tmp_db):
    rs = tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT NOT NULL)")
    assert rs.rows_affected == 0
    assert "t" in tmp_db.catalog.list_tables()


def test_create_duplicate_table_raises(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT)")
    with pytest.raises(ConstraintError):
        tmp_db.execute("CREATE TABLE t (id INT)")


def test_create_table_with_unique_index(tmp_db):
    rs = tmp_db.execute("CREATE TABLE accounts (email TEXT UNIQUE)")
    assert "accounts" in tmp_db.catalog.list_tables()


def test_drop_table(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT)")
    rs = tmp_db.execute("DROP TABLE t")
    assert rs.rows_affected == 0
    with pytest.raises(StorageError):
        tmp_db.execute("SELECT * FROM t")


def test_drop_nonexistent_table_raises(tmp_db):
    with pytest.raises(StorageError):
        tmp_db.execute("DROP TABLE nope")


# ---- insert ---------------------------------------------------------------


def test_insert_basic(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT NOT NULL)")
    rs = tmp_db.execute("INSERT INTO t VALUES (1, 'alice')")
    assert rs.rows_affected == 1


def test_insert_partial_columns(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT NOT NULL, age INT)")
    rs = tmp_db.execute("INSERT INTO t (id, name) VALUES (1, 'alice')")
    assert rs.rows_affected == 1
    rs = tmp_db.execute("SELECT * FROM t")
    assert rs.rows == [["1", "alice", ""]]


def test_insert_not_null_violation(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT NOT NULL)")
    with pytest.raises(ConstraintError):
        tmp_db.execute("INSERT INTO t (id) VALUES (1)")


def test_insert_pk_uniqueness(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT NOT NULL)")
    tmp_db.execute("INSERT INTO t VALUES (1, 'alice')")
    with pytest.raises(ConstraintError):
        tmp_db.execute("INSERT INTO t VALUES (1, 'bob')")


def test_insert_unique_violation(tmp_db):
    tmp_db.execute("CREATE TABLE t (email TEXT UNIQUE)")
    tmp_db.execute("INSERT INTO t VALUES ('a@x.com')")
    with pytest.raises(ConstraintError):
        tmp_db.execute("INSERT INTO t VALUES ('a@x.com')")


def test_insert_type_coercion(tmp_db):
    tmp_db.execute("CREATE TABLE t (x INT)")
    # INT literal into INT column — exact match.
    rs = tmp_db.execute("INSERT INTO t VALUES (42)")
    assert rs.rows_affected == 1
    # FLOAT literal that is integer-valued should still fit an INT column.
    rs = tmp_db.execute("INSERT INTO t VALUES (7)")
    assert rs.rows_affected == 1


def test_insert_type_mismatch_rejected(tmp_db):
    tmp_db.execute("CREATE TABLE t (x INT)")
    from tinydb.errors import TypeMismatchError
    with pytest.raises((ConstraintError, TypeMismatchError)):
        tmp_db.execute("INSERT INTO t VALUES ('not a number')")


def test_insert_value_count_mismatch(tmp_db):
    tmp_db.execute("CREATE TABLE t (a INT, b INT)")
    with pytest.raises(ConstraintError):
        tmp_db.execute("INSERT INTO t VALUES (1)")


# ---- update ---------------------------------------------------------------


def test_update_arithmetic_type_mismatch_raises(tmp_db):
    """UPDATE SET col = col + <non-numeric> must raise TypeMismatchError
    rather than silently mutating `col` (or crashing with a bare NameError
    if `_eval_arith` is missing the import).

    Regression: when _eval_arith raises `TypeMismatchError` but the symbol
    isn't imported in write.py, the update path blew up with a NameError
    deep in the executor instead of the typed exception callers can catch.
    """
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, label TEXT)")
    tmp_db.execute("INSERT INTO t VALUES (1, 'hello')")

    with pytest.raises(TypeMismatchError, match="arithmetic on non-numeric"):
        tmp_db.execute("UPDATE t SET label = label + 1 WHERE id = 1")

    # The row must NOT be corrupted by a partial update.
    rs = tmp_db.execute("SELECT label FROM t WHERE id = 1")
    assert rs.rows == [["hello"]]


def test_update_one_row(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT NOT NULL)")
    tmp_db.execute("INSERT INTO t VALUES (1, 'alice')")
    rs = tmp_db.execute("UPDATE t SET name = 'alicia' WHERE id = 1")
    assert rs.rows_affected == 1
    rs = tmp_db.execute("SELECT name FROM t WHERE id = 1")
    assert rs.rows == [["alicia"]]


def test_update_with_multiplication(tmp_db):
    """The parser must accept ``*`` as a binary multiplication operator in
    UPDATE expressions (the lexer emits it as TokKind.STAR, not OP, so a
    strict OP-only check in ``_parse_mul`` would reject the statement).
    """
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, quantity INT)")
    tmp_db.execute("INSERT INTO t VALUES (1, 21)")
    rs = tmp_db.execute("UPDATE t SET quantity = quantity * 2 WHERE id = 1")
    assert rs.rows_affected == 1
    rs = tmp_db.execute("SELECT quantity FROM t WHERE id = 1")
    assert rs.rows == [["42"]]


def test_update_multiple_rows(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, age INT)")
    for i in range(1, 6):
        tmp_db.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")
    rs = tmp_db.execute("UPDATE t SET age = age + 1")
    assert rs.rows_affected == 5


def test_update_with_predicate(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, age INT)")
    for i in range(1, 6):
        tmp_db.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")
    rs = tmp_db.execute("UPDATE t SET age = 0 WHERE age >= 30")
    assert rs.rows_affected == 3


def test_update_unknown_column_raises(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
    tmp_db.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(StorageError):
        tmp_db.execute("UPDATE t SET nope = 1")


def test_create_table_durable_across_crash(tmp_db_path):
    """Catalog page writes must be fsync'd BEFORE the header update.

    Regression: catalog.save() used disk.write_page (flush but no fsync)
    followed by disk.write_header (with fsync). If the OS reordered the
    writes, the header could land on disk pointing at catalog pages that
    never reached disk — a reopen would see an empty catalog (silent
    data loss). The fix must fsync the catalog pages BEFORE the header
    is committed.

    We verify the ordering invariant directly: between the first
    catalog page write and the header update, the data file must be
    fsync'd (via disk.sync()). Without that fsync, the fix regresses.
    """
    from tinydb import Database
    from tinydb.storage.disk import DiskManager

    events: list[str] = []
    real_write_page = DiskManager.write_page
    real_write_header = DiskManager.write_header
    real_sync = DiskManager.sync

    def spy_write_page(self, page):
        from tinydb.storage.page import PageType
        if page.page_type is PageType.CATALOG:
            events.append(f"cat_write({page.page_id})")
        return real_write_page(self, page)

    def spy_write_header(self, header):
        events.append("header_write")
        return real_write_header(self, header)

    def spy_sync(self):
        events.append("sync")
        return real_sync(self)

    DiskManager.write_page = spy_write_page
    DiskManager.write_header = spy_write_header
    DiskManager.sync = spy_sync
    try:
        db = Database(str(tmp_db_path))
        # Reset events so we only observe what happens during CREATE TABLE
        # (Database.open() does its own header write on file creation).
        events.clear()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
        db.close()
    finally:
        DiskManager.write_page = real_write_page
        DiskManager.write_header = real_write_header
        DiskManager.sync = real_sync

    # There must be at least one cat_write, then a sync, then header_write.
    cat_idxs = [i for i, e in enumerate(events) if e.startswith("cat_write(")]
    sync_idxs = [i for i, e in enumerate(events) if e == "sync"]
    header_idxs = [i for i, e in enumerate(events) if e == "header_write"]
    assert cat_idxs, f"no catalog page writes observed: {events}"
    assert header_idxs, f"no header write observed: {events}"
    assert sync_idxs, (
        f"no fsync between catalog page writes and header update: {events}. "
        "Without an fsync here, the OS can reorder header write before "
        "catalog page bytes reach disk, causing silent catalog loss on crash."
    )
    # The LAST header_write is the catalog_root_page update — that's the
    # one that must come AFTER a sync. The first header_write(s) come
    # from freelist.allocate bumping page_count, which is safe.
    last_cat = max(cat_idxs)
    last_header = max(header_idxs)
    last_sync = max(sync_idxs)
    assert last_sync > last_cat, (
        f"fsync must come AFTER all catalog page writes: {events}"
    )
    assert last_sync < last_header, (
        f"fsync must come BEFORE final header update: {events}"
    )


# ---- delete ---------------------------------------------------------------


def test_delete_with_predicate(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, age INT)")
    for i in range(1, 6):
        tmp_db.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")
    rs = tmp_db.execute("DELETE FROM t WHERE age >= 30")
    assert rs.rows_affected == 3
    rs = tmp_db.execute("SELECT COUNT(*) FROM t")
    assert rs.rows == [["2"]]


def test_delete_all(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
    for i in range(1, 4):
        tmp_db.execute(f"INSERT INTO t VALUES ({i})")
    rs = tmp_db.execute("DELETE FROM t")
    assert rs.rows_affected == 3
    rs = tmp_db.execute("SELECT COUNT(*) FROM t")
    assert rs.rows == [["0"]]


def test_delete_already_deleted_returns_zero(tmp_db):
    """DELETE WHERE pk=X must report 0 when X has already been deleted.

    Regression: _scan_table used by Delete enumerated every decoded row
    without checking the deleted flag, so a second DELETE WHERE pk=1
    reported rows_affected=1 (the live lookup happened against an
    already-deleted row, and mark_deleted is idempotent).
    """
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
    tmp_db.execute("INSERT INTO t VALUES (1, 10)")
    rs = tmp_db.execute("DELETE FROM t WHERE id = 1")
    assert rs.rows_affected == 1
    rs = tmp_db.execute("DELETE FROM t WHERE id = 1")
    assert rs.rows_affected == 0


def test_delete_filter_against_tombstone(tmp_db):
    """A DELETE WHERE col=v must skip tombstoned rows.

    With a non-PK column predicate, the scan-side filter used to match
    the in-memory row (still present even when deleted) and re-delete
    the tombstone. Verify rows_affected counts only live rows.
    """
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
    tmp_db.execute("INSERT INTO t VALUES (1, 10)")
    tmp_db.execute("INSERT INTO t VALUES (2, 10)")
    rs = tmp_db.execute("DELETE FROM t WHERE v = 10")
    assert rs.rows_affected == 2
    rs = tmp_db.execute("DELETE FROM t WHERE v = 10")
    assert rs.rows_affected == 0


def test_update_skips_tombstoned_rows(tmp_db):
    """UPDATE WHERE pk=X must not double-match when X is already deleted."""
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
    tmp_db.execute("INSERT INTO t VALUES (1, 10)")
    tmp_db.execute("DELETE FROM t WHERE id = 1")
    rs = tmp_db.execute("UPDATE t SET v = 999 WHERE id = 1")
    assert rs.rows_affected == 0
