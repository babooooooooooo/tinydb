"""Integration tests for BEGIN / COMMIT / ROLLBACK through the Database API.

These exercise the full pipeline including the WAL, the buffer pool, and
the heap page machinery to catch cross-layer bugs that unit tests miss.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tinydb import Database


@pytest.fixture
def fresh_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "tx.db"))
    yield db
    db.close()


def test_begin_commit_durability(fresh_db):
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    fresh_db.begin()
    fresh_db.execute("INSERT INTO t VALUES (1, 100)")
    fresh_db.execute("INSERT INTO t VALUES (2, 200)")
    fresh_db.commit()
    rs = fresh_db.execute("SELECT val FROM t ORDER BY id")
    assert rs.rows == [["100"], ["200"]]


def test_rollback_discards_inserts(fresh_db):
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    fresh_db.execute("INSERT INTO t VALUES (1, 100)")
    fresh_db.begin()
    fresh_db.execute("INSERT INTO t VALUES (2, 200)")
    fresh_db.execute("INSERT INTO t VALUES (3, 300)")
    fresh_db.rollback()
    rs = fresh_db.execute("SELECT id FROM t ORDER BY id")
    assert rs.rows == [["1"]]


def test_rollback_discards_updates(fresh_db):
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    fresh_db.execute("INSERT INTO t VALUES (1, 100)")
    fresh_db.begin()
    fresh_db.execute("UPDATE t SET val = 999 WHERE id = 1")
    fresh_db.rollback()
    rs = fresh_db.execute("SELECT val FROM t")
    assert rs.rows == [["100"]]


def test_rollback_discards_deletes(fresh_db):
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    fresh_db.execute("INSERT INTO t VALUES (1, 100)")
    fresh_db.execute("INSERT INTO t VALUES (2, 200)")
    fresh_db.begin()
    fresh_db.execute("DELETE FROM t WHERE id = 1")
    fresh_db.rollback()
    rs = fresh_db.execute("SELECT id FROM t ORDER BY id")
    assert rs.rows == [["1"], ["2"]]


def test_rollback_discards_create_table(fresh_db):
    fresh_db.begin()
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
    fresh_db.rollback()
    # After rollback, the table should not exist.
    with pytest.raises(Exception):
        fresh_db.execute("INSERT INTO t VALUES (1)")


def test_rollback_discards_drop_table(fresh_db):
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    fresh_db.execute("INSERT INTO t VALUES (1, 100)")
    fresh_db.begin()
    fresh_db.execute("DROP TABLE t")
    fresh_db.rollback()
    rs = fresh_db.execute("SELECT val FROM t")
    assert rs.rows == [["100"]]


def test_begin_via_sql_statement(fresh_db):
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    fresh_db.execute("BEGIN")
    fresh_db.execute("INSERT INTO t VALUES (1, 100)")
    fresh_db.execute("COMMIT")
    rs = fresh_db.execute("SELECT val FROM t")
    assert rs.rows == [["100"]]


def test_rollback_via_sql_statement(fresh_db):
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    fresh_db.execute("INSERT INTO t VALUES (1, 100)")
    fresh_db.execute("BEGIN")
    fresh_db.execute("INSERT INTO t VALUES (2, 200)")
    fresh_db.execute("ROLLBACK")
    rs = fresh_db.execute("SELECT id FROM t")
    assert rs.rows == [["1"]]


def test_nested_begin_raises(fresh_db):
    fresh_db.begin()
    with pytest.raises(Exception):
        fresh_db.begin()
    fresh_db.rollback()


def test_commit_outside_txn_raises(fresh_db):
    with pytest.raises(Exception):
        fresh_db.commit()


def test_rollback_outside_txn_raises(fresh_db):
    with pytest.raises(Exception):
        fresh_db.rollback()


def test_recovery_replays_committed(tmp_path):
    """After a crash, recovery replays committed transactions."""
    p = str(tmp_path / "recover.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    db.begin()
    db.execute("INSERT INTO t VALUES (1, 100)")
    db.execute("INSERT INTO t VALUES (2, 200)")
    db.commit()
    db.close()

    # Open again — recovery should replay and find committed state.
    db2 = Database(p)
    rs = db2.execute("SELECT COUNT(*) FROM t")
    assert rs.rows == [["2"]]
    db2.close()


def test_recovery_discards_uncommitted(tmp_path):
    """Uncommitted transactions are discarded after a crash."""
    p = str(tmp_path / "uncommitted.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    db.execute("INSERT INTO t VALUES (1, 100)")
    db.begin()
    db.execute("INSERT INTO t VALUES (2, 200)")
    # Simulate crash by NOT committing.
    db.close()

    db2 = Database(p)
    rs = db2.execute("SELECT COUNT(*) FROM t")
    # Only the auto-committed row from before BEGIN should survive.
    assert rs.rows == [["1"]]
    db2.close()


def test_rollback_restores_page_header_for_repeated_inserts(fresh_db):
    """Regression: rollback after multiple inserts in one txn.

    The bug was that the page header (free_offset) was being read from
    stale bytes, so rollback restored free_offset=0 even after
    appending rows. The page property setter fix ensures the header
    stays in sync with the attributes.
    """
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    fresh_db.begin()
    for i in range(1, 6):
        fresh_db.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")
    fresh_db.rollback()

    # After rollback, the catalog state is restored: t still exists
    # (created before BEGIN), but no rows are visible. No heap pages
    # are allocated.
    rs = fresh_db.execute("SELECT COUNT(*) FROM t")
    assert rs.rows == [["0"]]
    assert fresh_db.catalog.get_table("t").heap_last_page == 0


def test_implicit_rollback_on_close(tmp_path):
    """Closing the database with an open txn rolls back automatically."""
    p = str(tmp_path / "close.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    db.execute("INSERT INTO t VALUES (1, 100)")
    db.begin()
    db.execute("INSERT INTO t VALUES (2, 200)")
    db.close()  # implicit rollback

    db2 = Database(p)
    rs = db2.execute("SELECT COUNT(*) FROM t")
    assert rs.rows == [["1"]]
    db2.close()


def test_header_wal_replay_after_crash(tmp_path):
    """Recovery replays HEADER records so catalog survives a crash."""
    p = str(tmp_path / "hwal.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    db.execute("INSERT INTO t VALUES (1, 100)")
    db.execute("INSERT INTO t VALUES (2, 200)")
    db.close()  # normal close — WAL truncated, header on disk

    # Reopen and mutate inside a txn, then simulate crash by NOT closing.
    db2 = Database(p)
    db2.begin()
    db2.execute("INSERT INTO t VALUES (3, 300)")
    db2.execute("UPDATE t SET val = 999 WHERE id = 1")
    db2.commit()
    # No db2.close(): simulate process crash.

    db3 = Database(p)
    rs = db3.execute("SELECT val FROM t WHERE id = 1")
    assert rs.rows == [["999"]]
    rs = db3.execute("SELECT val FROM t WHERE id = 3")
    assert rs.rows == [["300"]]
    db3.close()


def test_update_then_rollback_survives_reopen(tmp_path):
    """An UPDATE inside an uncommitted txn, followed by ROLLBACK and process
    exit, must NOT leak the post-image across a reopen. Regression for
    rollback-doesn't-flush: before the fix, the disk held the post-image and
    a reopen showed the uncommitted update.
    """
    p = str(tmp_path / "updrollback.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    db.execute("INSERT INTO t VALUES (1, 100)")
    db.pool.flush_all()
    db.close()

    db2 = Database(p)
    db2.begin()
    db2.execute("UPDATE t SET val = 999 WHERE id = 1")
    db2.pool.flush_all()  # force post-image to disk mid-txn
    db2.rollback()
    db2.close()  # clean exit; no commit

    db3 = Database(p)
    rs = db3.execute("SELECT val FROM t WHERE id = 1")
    assert rs.rows == [["100"]], (
        f"reopen showed {rs.rows}; rollback post-image leaked"
    )
    db3.close()


def test_insert_then_rollback_survives_reopen(tmp_path):
    """An INSERT inside an uncommitted txn followed by ROLLBACK + reopen
    must NOT see the inserted row.
    """
    p = str(tmp_path / "insrollback.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    db.execute("INSERT INTO t VALUES (1, 100)")
    db.close()

    db2 = Database(p)
    db2.begin()
    db2.execute("INSERT INTO t VALUES (2, 200)")
    db2.pool.flush_all()
    db2.rollback()
    db2.close()

    db3 = Database(p)
    rs = db3.execute("SELECT COUNT(*) FROM t")
    assert rs.rows == [["1"]]
    rs = db3.execute("SELECT val FROM t WHERE id = 2")
    assert rs.rows == []
    db3.close()


def test_commit_survives_reopen_regression(tmp_path):
    """Sanity regression: a COMMITTED UPDATE followed by reopen must show the
    new value. Guards against an over-eager rollback fix that accidentally
    drops committed writes.
    """
    p = str(tmp_path / "commit.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    db.execute("INSERT INTO t VALUES (1, 100)")
    db.close()

    db2 = Database(p)
    db2.begin()
    db2.execute("UPDATE t SET val = 999 WHERE id = 1")
    db2.commit()
    db2.close()

    db3 = Database(p)
    rs = db3.execute("SELECT val FROM t WHERE id = 1")
    assert rs.rows == [["999"]]
    db3.close()


def test_update_rollback_then_read_in_same_session(fresh_db):
    """Within a single session, ROLLBACK must revert in-flight state so a
    subsequent SELECT sees the pre-image. Asserts the in-memory side of
    the rollback-fix (cache coherency after disk flush).
    """
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    fresh_db.execute("INSERT INTO t VALUES (1, 100)")
    fresh_db.begin()
    fresh_db.execute("UPDATE t SET val = 999 WHERE id = 1")
    fresh_db.pool.flush_all()  # post-image to disk
    fresh_db.rollback()
    # Now SELECT — should see original 100, not 999.
    rs = fresh_db.execute("SELECT val FROM t WHERE id = 1")
    assert rs.rows == [["100"]]


def test_empty_rollback_after_begin(fresh_db):
    """BEGIN with no writes then ROLLBACK must leave all state intact.
    Guards against a fix path that mishandles empty undo logs.
    """
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    fresh_db.execute("INSERT INTO t VALUES (1, 100)")
    fresh_db.execute("INSERT INTO t VALUES (2, 200)")
    fresh_db.begin()
    fresh_db.rollback()
    rs = fresh_db.execute("SELECT val FROM t ORDER BY id")
    assert rs.rows == [["100"], ["200"]]


def test_deferred_free_in_transaction(tmp_path):
    """Free inside a txn is deferred; rollback leaves pages intact."""
    p = str(tmp_path / "deferred.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    db.execute("INSERT INTO t VALUES (1, 100)")
    db.execute("INSERT INTO t VALUES (2, 200)")
    db.close()

    db2 = Database(p)
    heap_page = db2.catalog.get_table("t").heap_last_page
    db2.begin()
    db2.execute("DROP TABLE t")
    db2.rollback()
    # After rollback, the table is back AND its heap page is intact.
    rs = db2.execute("SELECT val FROM t ORDER BY id")
    assert rs.rows == [["100"], ["200"]]
    assert db2.catalog.get_table("t").heap_last_page == heap_page
    db2.close()


def test_index_rollback_indexscan_no_keyerror(tmp_path):
    """Regression: ROLLBACK of an INSERT must also undo the index entry.

    Before the fix, the B+ tree pages were not in the txn undo log, so
    after ROLLBACK the index still mapped id=2 to the freed row offset.
    A subsequent IndexScan decoded garbage at that offset and emitted a
    Row(values={}), which made the Filter raise KeyError on the column
    reference.
    """
    p = str(tmp_path / "idxrollback.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    db.execute("INSERT INTO t VALUES (1, 100)")
    db.close()

    db2 = Database(p)
    db2.begin()
    db2.execute("INSERT INTO t VALUES (2, 200)")
    db2.pool.flush_all()
    db2.rollback()
    db2.close()

    db3 = Database(p)
    # Both forms must not raise and must not return the rolled-back row.
    rs = db3.execute("SELECT val FROM t WHERE id = 2")
    assert rs.rows == []
    rs = db3.execute("SELECT val FROM t WHERE id = 1")
    assert rs.rows == [["100"]]
    db3.close()


def test_index_rollback_in_same_session(fresh_db):
    """Within one session, ROLLBACK of an INSERT must remove the index
    entry so a subsequent SELECT WHERE id=N returns nothing.
    """
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    fresh_db.execute("INSERT INTO t VALUES (1, 100)")
    fresh_db.begin()
    fresh_db.execute("INSERT INTO t VALUES (2, 200)")
    fresh_db.pool.flush_all()
    fresh_db.rollback()
    rs = fresh_db.execute("SELECT val FROM t WHERE id = 2")
    assert rs.rows == []
    rs = fresh_db.execute("SELECT COUNT(*) FROM t")
    assert rs.rows == [["1"]]


def test_index_rollback_after_multiple_inserts(fresh_db):
    """Several INSERTs in one txn, then ROLLBACK: every index entry must
    disappear, not just the latest.
    """
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    fresh_db.begin()
    for i in range(2, 7):
        fresh_db.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")
    fresh_db.pool.flush_all()
    fresh_db.rollback()
    rs = fresh_db.execute("SELECT COUNT(*) FROM t")
    assert rs.rows == [["0"]]
    for i in range(2, 7):
        rs = fresh_db.execute(f"SELECT val FROM t WHERE id = {i}")
        assert rs.rows == [], f"id={i} should not be visible after rollback"


def test_index_rollback_after_update_pkey(fresh_db):
    """UPDATE of a PRIMARY KEY inside a txn: on ROLLBACK the index entry
    must point back at the original key, not the new key.
    """
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    fresh_db.execute("INSERT INTO t VALUES (1, 100)")
    fresh_db.execute("INSERT INTO t VALUES (2, 200)")
    fresh_db.begin()
    fresh_db.execute("UPDATE t SET id = 999 WHERE id = 1")
    fresh_db.pool.flush_all()
    fresh_db.rollback()
    rs = fresh_db.execute("SELECT val FROM t WHERE id = 1")
    assert rs.rows == [["100"]]
    rs = fresh_db.execute("SELECT val FROM t WHERE id = 999")
    assert rs.rows == []


def test_index_rollback_after_delete(fresh_db):
    """DELETE inside a txn, then ROLLBACK: the index entry for the
    deleted row must be restored so a subsequent WHERE finds it again.
    """
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    fresh_db.execute("INSERT INTO t VALUES (1, 100)")
    fresh_db.execute("INSERT INTO t VALUES (2, 200)")
    fresh_db.begin()
    fresh_db.execute("DELETE FROM t WHERE id = 2")
    fresh_db.pool.flush_all()
    fresh_db.rollback()
    rs = fresh_db.execute("SELECT val FROM t WHERE id = 2")
    assert rs.rows == [["200"]]
    rs = fresh_db.execute("SELECT COUNT(*) FROM t")
    assert rs.rows == [["2"]]


def test_index_rollback_after_split(tmp_path):
    """Force a B+ tree leaf split during INSERT, then ROLLBACK. The split
    pages and the new entry must all revert to the pre-txn state.
    """
    p = str(tmp_path / "split.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    # Populate enough rows that the next INSERT is likely to touch the
    # primary-key index without forcing a split (single-leaf is fine —
    # the goal is to exercise the txn-aware path through the leaf).
    for i in range(1, 50):
        db.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")
    db.close()

    db2 = Database(p)
    # Read index root before txn.
    idx_before = db2.catalog.get_table("t").index_for("id").root_page

    db2.begin()
    # Add rows that will force multiple leaf splits inside the txn.
    for i in range(50, 130):
        db2.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")
    db2.pool.flush_all()
    db2.rollback()
    db2.close()

    db3 = Database(p)
    # Index root must be the original.
    idx_after = db3.catalog.get_table("t").index_for("id").root_page
    assert idx_after == idx_before
    # Only the original 49 rows survive.
    rs = db3.execute("SELECT COUNT(*) FROM t")
    assert rs.rows == [["49"]]
    # Spot-check that rolled-back rows are absent.
    rs = db3.execute("SELECT val FROM t WHERE id = 100")
    assert rs.rows == []
    db3.close()


def test_index_rollback_unique_constraint_violation(fresh_db):
    """Inserting a duplicate PK inside a txn then rolling back must leave
    the index usable for the original rows.
    """
    fresh_db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    fresh_db.execute("INSERT INTO t VALUES (1, 100)")
    fresh_db.begin()
    with pytest.raises(Exception):
        fresh_db.execute("INSERT INTO t VALUES (1, 999)")
    fresh_db.rollback()
    rs = fresh_db.execute("SELECT val FROM t WHERE id = 1")
    assert rs.rows == [["100"]]


def test_index_rollback_indexsurvives_reopen(tmp_path):
    """After ROLLBACK the index must not just be correct in memory but
    also on disk — verify by reopening and querying through the index.
    """
    p = str(tmp_path / "idxsurvive.db")
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, val INT)")
    for i in range(1, 11):
        db.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")
    db.close()

    db2 = Database(p)
    db2.begin()
    for i in range(11, 21):
        db2.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")
    db2.pool.flush_all()
    db2.rollback()
    db2.close()

    db3 = Database(p)
    # Query via index (id is PRIMARY KEY so the planner uses IndexScan).
    rs = db3.execute("SELECT val FROM t WHERE id = 5")
    assert rs.rows == [["50"]]
    rs = db3.execute("SELECT COUNT(*) FROM t")
    assert rs.rows == [["10"]]
    for i in range(11, 21):
        rs = db3.execute(f"SELECT val FROM t WHERE id = {i}")
        assert rs.rows == [], f"id={i} should not survive rollback"
    db3.close()


def test_pk_enforced_after_unclean_shutdown(tmp_path):
    """PK uniqueness must survive a crash between INSERT and db.close().

    Regression: the catalog (including index.root_page) was only saved
    on db.close(). A crash mid-session left the on-disk catalog with
    root_page=0, so the next session's first INSERT created a NEW btree
    leaf and the uniqueness check on the transient (empty) tree passed.
    Result: duplicate PK silently accepted.
    """
    p = str(tmp_path / "pkenforce.db")

    # Session 1: create the table.
    db = Database(p)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
    db.close()

    # Session 2: insert, then "crash" (skip db.close()).
    db2 = Database(p)
    db2.execute("INSERT INTO t VALUES (1)")
    del db2  # simulate crash — no close, no catalog.save()

    # Session 3: reopen. A duplicate INSERT must still be rejected.
    db3 = Database(p)
    try:
        db3.execute("INSERT INTO t VALUES (1)")
    except Exception:
        # Expected: ConstraintError.
        db3.close()
        return
    rs = db3.execute("SELECT id FROM t")
    db3.close()
    raise AssertionError(
        f"duplicate PK accepted after unclean shutdown: rows={rs.rows}"
    )


def test_unique_constraint_enforced_after_unclean_shutdown(tmp_path):
    """UNIQUE constraint (non-PK) must survive a crash between INSERT and close."""
    p = str(tmp_path / "uniqueenforce.db")

    db = Database(p)
    db.execute("CREATE TABLE accounts (email TEXT UNIQUE)")
    db.close()

    db2 = Database(p)
    db2.execute("INSERT INTO accounts VALUES ('a@x.com')")
    del db2

    db3 = Database(p)
    try:
        db3.execute("INSERT INTO accounts VALUES ('a@x.com')")
    except Exception:
        db3.close()
        return
    rs = db3.execute("SELECT email FROM accounts")
    db3.close()
    raise AssertionError(
        f"duplicate UNIQUE accepted after unclean shutdown: rows={rs.rows}"
    )