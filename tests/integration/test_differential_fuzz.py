"""Differential / model-based SQL tests.

These tests follow the technique used by SQLite's test suite: every SQL
statement the database executes is mirrored on a tiny in-memory Python
reference model, and after each operation the two are compared. Random
sequences of INSERT / UPDATE / DELETE / SELECT run for many steps; a
hidden bug in either the executor or the storage layer usually surfaces
as a divergence between the model's expected result and the database's.

Two structural things to know:

* Results from ``Database.execute`` carry every value as a **string**
  (per ``Database._format_value``). INT → ``str(int)``; FLOAT → ``repr``
  unless the value is integer-valued, in which case it round-trips as
  an int string; TEXT → ``str``; NULL → ``""``. The model uses raw
  Python values, so the helper ``_stringify`` mirrors that mapping.

* Constraint failures (``ConstraintError`` for PK / UNIQUE / NOT NULL)
  happen at INSERT and UPDATE time. The reference model mirrors the
  same rules so we can compare. We deliberately do **not** catch every
  error — only the constraint classes that have a deterministic,
  model-checkable outcome. Anything else (``StorageError``,
  ``ParseError``) is a real bug and must surface.

These tests are intentionally deterministic: ``random.seed(42)`` so a
flaky run reproduces.
"""

from __future__ import annotations

import random
from typing import Any, Callable

import pytest

from tinydb import Database
from tinydb.errors import ConstraintError


# ---------------------------------------------------------------------------
# Reference model
# ---------------------------------------------------------------------------


class ReferenceModel:
    """A small in-memory table mirroring the schema we fuzz against.

    Schema is fixed for these tests:

        pk  INT PRIMARY KEY
        a   INT
        b   TEXT
        c   INT          -- nullable, used for NULL semantics

    The model enforces the same constraints tinydb enforces (PK
    uniqueness, NOT NULL on ``a``/``b``) so that constraint-violation
    behaviour is symmetric.
    """

    SCHEMA = ("pk", "a", "b", "c")
    NOT_NULL = ("pk", "a", "b")

    def __init__(self) -> None:
        self._rows: dict[int, dict[str, Any]] = {}

    # ---- INSERT ----------------------------------------------------------

    def insert(self, pk: int, a: int, b: str, c: int | None) -> None:
        if pk in self._rows:
            raise ConstraintError(f"duplicate PK {pk}")
        row = {"pk": pk, "a": a, "b": b, "c": c}
        for col in self.NOT_NULL:
            if row[col] is None:
                raise ConstraintError(f"NOT NULL violation on {col}")
        self._rows[pk] = row

    # ---- UPDATE / DELETE -------------------------------------------------

    def update_where_eq(self, col: str, value: Any, set_col: str, set_val: Any) -> int:
        n = 0
        for row in self._rows.values():
            if row[col] == value:
                if set_col in self.NOT_NULL and set_val is None:
                    raise ConstraintError(f"NOT NULL violation on {set_col}")
                row[set_col] = set_val
                n += 1
        return n

    def delete_where_eq(self, col: str, value: Any) -> int:
        keys = [pk for pk, row in self._rows.items() if row[col] == value]
        for pk in keys:
            del self._rows[pk]
        return len(keys)

    # ---- SELECT ----------------------------------------------------------

    def all_rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows.values()]

    def select_where_eq(self, col: str, value: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows.values() if row_predicate_eq(r, col, value)]

    def select_order_by(self, col: str) -> list[dict[str, Any]]:
        # NULL sorts LAST in ASC order — matches tinydb's documented behaviour.
        rows = self.all_rows()
        rows.sort(key=lambda r: (r[col] is None, r[col]))
        return rows

    def count(self) -> int:
        return len(self._rows)

    def sum_a(self) -> int | None:
        """SUM(a) over the table, NULL if no rows."""
        if not self._rows:
            return None
        return sum(r["a"] for r in self._rows.values())

    def sum_c(self) -> int | None:
        """SUM(c) ignoring NULL operands; NULL if no non-NULL contributions."""
        total = 0
        saw = False
        for r in self._rows.values():
            if r["c"] is not None:
                total += r["c"]
                saw = True
        return total if saw else None

    def group_by_b_sum_a(self) -> list[tuple[str, int | None]]:
        groups: dict[str, list[int]] = {}
        for r in self._rows.values():
            groups.setdefault(r["b"], []).append(r["a"])
        return [(b, sum(vs)) for b, vs in sorted(groups.items())]


def row_predicate_eq(row: dict[str, Any], col: str, value: Any) -> bool:
    """SQL = with three-valued logic: NULL = NULL is UNKNOWN → no match."""
    lhs = row[col]
    if lhs is None or value is None:
        return False
    return lhs == value


# ---------------------------------------------------------------------------
# Stringification (mirror Database._format_value)
# ---------------------------------------------------------------------------


def _stringify(v: Any) -> str:
    """Mirror ``Database._format_value``: ints as ``str(int)``,
    float-valued ints render as int string, real floats via ``repr``,
    NULL → empty string, everything else via ``str``.
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v == int(v):
            return str(int(v))
        return repr(v)
    return str(v)


def _stringify_rows(rows: list[dict[str, Any]], cols: tuple[str, ...]) -> list[list[str]]:
    return [[_stringify(r[c]) for c in cols] for r in rows]


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


def _bootstrap(db: Database) -> None:
    db.execute(
        "CREATE TABLE t (pk INT PRIMARY KEY, a INT NOT NULL, b TEXT NOT NULL, c INT)"
    )


# ---------------------------------------------------------------------------
# SQL helpers used by the fuzzer
# ---------------------------------------------------------------------------


def _do_insert(
    db: Database, model: ReferenceModel, pk: int, a: int, b: str, c: int | None
) -> bool:
    sql_c = "NULL" if c is None else str(c)
    sql = f"INSERT INTO t VALUES ({pk}, {a}, '{b}', {sql_c})"
    try:
        db.execute(sql)
    except ConstraintError:
        # PK / NOT NULL violation. Model must also reject.
        with pytest.raises(ConstraintError):
            model.insert(pk, a, b, c)
        return False
    model.insert(pk, a, b, c)
    return True


def _value_sql(v: Any) -> str:
    """Render a Python value as a SQL literal."""
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return str(v)


def _do_update_eq(
    db: Database,
    model: ReferenceModel,
    col: str,
    value: Any,
    set_col: str,
    set_val: Any,
) -> int:
    sql = (
        f"UPDATE t SET {set_col} = {_value_sql(set_val)} "
        f"WHERE {col} = {_value_sql(value)}"
    )
    rs = db.execute(sql)
    n_model = model.update_where_eq(col, value, set_col, set_val)
    assert rs.rows_affected == n_model, (
        f"UPDATE rows_affected={rs.rows_affected}, model={n_model} "
        f"(SQL: {sql})"
    )
    return rs.rows_affected


def _do_delete_eq(db: Database, model: ReferenceModel, col: str, value: Any) -> int:
    sql = f"DELETE FROM t WHERE {col} = {_value_sql(value)}"
    rs = db.execute(sql)
    n_model = model.delete_where_eq(col, value)
    assert rs.rows_affected == n_model, (
        f"DELETE rows_affected={rs.rows_affected}, model={n_model} (SQL: {sql})"
    )
    return rs.rows_affected


def _do_select_eq(
    db: Database, model: ReferenceModel, col: str, value: Any
) -> None:
    if isinstance(value, str):
        # SQL string literal: wrap in single quotes; escape any inner
        # single quotes by doubling them.
        escaped = value.replace("'", "''")
        value_sql = f"'{escaped}'"
    elif value is None:
        value_sql = "NULL"
    else:
        value_sql = str(value)
    sql = (
        f"SELECT pk, a, b, c FROM t WHERE {col} = {value_sql} ORDER BY pk"
    )
    rs = db.execute(sql)
    expected_rows = sorted(
        model.select_where_eq(col, value), key=lambda r: r["pk"]
    )
    expected = _stringify_rows(expected_rows, ("pk", "a", "b", "c"))
    assert rs.rows == expected, (
        f"SELECT diverged\n  SQL:    {rs.rows}\n  Model:  {expected}"
    )


def _do_select_all_order_by_pk(db: Database, model: ReferenceModel) -> None:
    rs = db.execute("SELECT pk, a, b, c FROM t ORDER BY pk")
    expected_rows = sorted(model.all_rows(), key=lambda r: r["pk"])
    expected = _stringify_rows(expected_rows, ("pk", "a", "b", "c"))
    assert rs.rows == expected, (
        f"SELECT * ORDER BY pk diverged\n  SQL:   {rs.rows}\n  Model: {expected}"
    )


def _do_count_star(db: Database, model: ReferenceModel) -> None:
    rs = db.execute("SELECT COUNT(*) FROM t")
    assert rs.rows == [[_stringify(model.count())]], (
        f"COUNT(*) diverged: sql={rs.rows}, model={model.count()}"
    )


def _do_sum_a(db: Database, model: ReferenceModel) -> None:
    rs = db.execute("SELECT SUM(a) FROM t")
    expected = model.sum_a()
    if expected is None:
        assert rs.rows == [[""]], f"SUM(a) over empty diverged: {rs.rows}"
    else:
        assert rs.rows == [[_stringify(expected)]], (
            f"SUM(a) diverged: sql={rs.rows}, model={expected}"
        )


def _do_sum_c(db: Database, model: ReferenceModel) -> None:
    rs = db.execute("SELECT SUM(c) FROM t")
    expected = model.sum_c()
    if expected is None:
        assert rs.rows == [[""]], f"SUM(c) (NULL result) diverged: {rs.rows}"
    else:
        assert rs.rows == [[_stringify(expected)]], (
            f"SUM(c) diverged: sql={rs.rows}, model={expected}"
        )


def _do_group_by_b_sum_a(db: Database, model: ReferenceModel) -> None:
    rs = db.execute(
        "SELECT b, SUM(a) FROM t GROUP BY b ORDER BY b"
    )
    expected = model.group_by_b_sum_a()
    expected_str = [[b, _stringify(s)] for b, s in expected]
    assert rs.rows == expected_str, (
        f"GROUP BY b, SUM(a) diverged\n  SQL:   {rs.rows}\n  Model: {expected_str}"
    )


# ---------------------------------------------------------------------------
# The fuzzer
# ---------------------------------------------------------------------------


def _random_int(rng: random.Random, lo: int = 1, hi: int = 50) -> int:
    return rng.randint(lo, hi)


def _random_text(rng: random.Random, alphabet: str = "abcde") -> str:
    n = rng.randint(1, 3)
    return "".join(rng.choice(alphabet) for _ in range(n))


def _op_insert(
    rng: random.Random, db: Database, model: ReferenceModel
) -> None:
    pk = _random_int(rng, 1, 30)
    # Avoid re-using PKs — most of the time. Sometimes we collide on
    # purpose so the model exercises the duplicate-PK path.
    if pk in model._rows and rng.random() < 0.7:
        pk = _random_int(rng, 1, 30)
    a = _random_int(rng, 1, 100)
    b = _random_text(rng)
    c: int | None
    c = _random_int(rng, 0, 10) if rng.random() > 0.25 else None
    _do_insert(db, model, pk, a, b, c)


_OPS: list[Callable[[random.Random, Database, ReferenceModel], None]] = [
    _op_insert,
    lambda r, db, m: _do_select_eq(db, m, "pk", _random_int(r, 1, 30)),
    lambda r, db, m: _do_select_eq(db, m, "a", _random_int(r, 1, 100)),
    lambda r, db, m: _do_select_eq(db, m, "b", _random_text(r)),
    lambda r, db, m: _do_select_all_order_by_pk(db, m),
    lambda r, db, m: _do_count_star(db, m),
    lambda r, db, m: _do_sum_a(db, m),
    lambda r, db, m: _do_sum_c(db, m),
    lambda r, db, m: _do_group_by_b_sum_a(db, m),
    lambda r, db, m: _do_update_eq(
        db, m, "a", _random_int(r, 1, 100), "a", _random_int(r, 1, 100)
    ),
    lambda r, db, m: _do_update_eq(
        db, m, "b", _random_text(r), "b", _random_text(r)
    ),
    lambda r, db, m: _do_delete_eq(db, m, "pk", _random_int(r, 1, 30)),
    lambda r, db, m: _do_delete_eq(db, m, "a", _random_int(r, 1, 100)),
]


# ---------------------------------------------------------------------------
# Record-and-replay (state-independent, for the persistence test)
# ---------------------------------------------------------------------------
#
# The live driver above consumes the rng inside each op, and one of the
# ops (``_op_insert``) consumes rng conditionally on model state. That
# makes the rng sequence state-dependent, which is fine for the in-
# memory differential tests (model and db are advanced in lockstep)
# but is a trap for persistence tests: replaying the rng against a
# reopened DB re-issues the same INSERTs, the DB raises ConstraintError
# for ones it already has, and the test silently passes even when
# persistence is broken.
#
# The recorder below is the state-independent counterpart: it consumes
# rng in a fixed order that does NOT depend on the model or the DB.
# The recorded op list can be replayed any number of times against
# any starting state and produces deterministic SQL + model output.


# Each kind is a string label. The order here is the rng.choice()
# order during recording — keep it stable.
_OP_KINDS: list[str] = [
    "insert",
    "select_eq_pk",
    "select_eq_a",
    "select_eq_b",
    "select_all",
    "count_star",
    "sum_a",
    "sum_c",
    "group_by_b_sum_a",
    "update_a",
    "update_b",
    "delete_eq_pk",
    "delete_eq_a",
]


def _record_ops(rng: random.Random, n_ops: int) -> list[tuple]:
    """Generate a list of (kind, *params) tuples, all state-independent
    of any database or model. Each tuple is enough to reproduce both
    the SQL statement and the corresponding model mutation.

    Mirrors ``_OPS`` 1:1 — every kind that can be live-driven is also
    recordable. The probability of an unconditional pk re-roll in
    ``_record_op_insert`` (0.3) is a rough match for the old
    state-dependent behavior in ``_op_insert``.
    """
    ops: list[tuple] = []
    for _ in range(n_ops):
        kind = rng.choice(_OP_KINDS)
        if kind == "insert":
            pk = _random_int(rng, 1, 30)
            # Unconditional random.random() so rng consumption is fixed
            # regardless of model state. Re-roll pk with probability 0.3.
            if rng.random() < 0.3:
                pk = _random_int(rng, 1, 30)
            a = _random_int(rng, 1, 100)
            b = _random_text(rng)
            c = _random_int(rng, 0, 10) if rng.random() > 0.25 else None
            ops.append(("insert", pk, a, b, c))
        elif kind == "select_eq_pk":
            ops.append(("select_eq_pk", _random_int(rng, 1, 30)))
        elif kind == "select_eq_a":
            ops.append(("select_eq_a", _random_int(rng, 1, 100)))
        elif kind == "select_eq_b":
            ops.append(("select_eq_b", _random_text(rng)))
        elif kind == "select_all":
            ops.append(("select_all",))
        elif kind == "count_star":
            ops.append(("count_star",))
        elif kind == "sum_a":
            ops.append(("sum_a",))
        elif kind == "sum_c":
            ops.append(("sum_c",))
        elif kind == "group_by_b_sum_a":
            ops.append(("group_by_b_sum_a",))
        elif kind == "update_a":
            ops.append(
                (
                    "update_a",
                    _random_int(rng, 1, 100),
                    _random_int(rng, 1, 100),
                )
            )
        elif kind == "update_b":
            ops.append(("update_b", _random_text(rng), _random_text(rng)))
        elif kind == "delete_eq_pk":
            ops.append(("delete_eq_pk", _random_int(rng, 1, 30)))
        elif kind == "delete_eq_a":
            ops.append(("delete_eq_a", _random_int(rng, 1, 100)))
        else:
            raise AssertionError(f"unknown recorded op kind: {kind}")
    return ops


def _replay_op(
    op: tuple, db: Database, model: ReferenceModel
) -> None:
    """Execute one recorded op against db + model.

    Mirrors the live op helpers 1:1 — INSERT/UPDATE/DELETE catch
    ConstraintError so the fuzzer can keep going (same as
    ``_do_insert`` etc.); SELECT compares SQL result to model.
    """
    kind = op[0]
    if kind == "insert":
        _do_insert(db, model, op[1], op[2], op[3], op[4])
    elif kind == "select_eq_pk":
        _do_select_eq(db, model, "pk", op[1])
    elif kind == "select_eq_a":
        _do_select_eq(db, model, "a", op[1])
    elif kind == "select_eq_b":
        _do_select_eq(db, model, "b", op[1])
    elif kind == "select_all":
        _do_select_all_order_by_pk(db, model)
    elif kind == "count_star":
        _do_count_star(db, model)
    elif kind == "sum_a":
        _do_sum_a(db, model)
    elif kind == "sum_c":
        _do_sum_c(db, model)
    elif kind == "group_by_b_sum_a":
        _do_group_by_b_sum_a(db, model)
    elif kind == "update_a":
        _do_update_eq(db, model, "a", op[1], "a", op[2])
    elif kind == "update_b":
        _do_update_eq(db, model, "b", op[1], "b", op[2])
    elif kind == "delete_eq_pk":
        _do_delete_eq(db, model, "pk", op[1])
    elif kind == "delete_eq_a":
        _do_delete_eq(db, model, "a", op[1])
    else:
        raise AssertionError(f"unknown recorded op kind: {kind}")


def _replay_ops(
    ops: list[tuple], db: Database, model: ReferenceModel
) -> None:
    for op in ops:
        _replay_op(op, db, model)


def _drive_random_session(
    rng: random.Random, db: Database, model: ReferenceModel, n_ops: int
) -> None:
    for _ in range(n_ops):
        op = rng.choice(_OPS)
        op(rng, db, model)


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_short_random_session_matches_model(tmp_db):
    """200 random SQL ops, model in lockstep.

    Catches off-by-one bugs in tombstoning, predicate evaluation, and
    aggregate reduction under interleaved writes/reads.
    """
    rng = random.Random(42)
    _bootstrap(tmp_db)
    model = ReferenceModel()
    _drive_random_session(rng, tmp_db, model, n_ops=200)
    # Final consistency check.
    _do_select_all_order_by_pk(tmp_db, model)
    _do_count_star(tmp_db, model)
    _do_sum_a(tmp_db, model)
    _do_sum_c(tmp_db, model)
    _do_group_by_b_sum_a(tmp_db, model)


def test_long_random_session_matches_model(tmp_db):
    """1000 random ops — stresses multi-page heap and B+ tree state.

    Larger pool of distinct PKs (1..200) so the heap grows beyond a
    single page; larger text alphabet to exercise more index entries.
    """
    rng = random.Random(2024)
    _bootstrap(tmp_db)
    model = ReferenceModel()
    for _ in range(1000):
        op = rng.choice(_OPS)
        if op is _op_insert:
            pk = _random_int(rng, 1, 200)
            a = _random_int(rng, 1, 500)
            b = _random_text(rng, alphabet="abcdefghij")
            c = _random_int(rng, 0, 50) if rng.random() > 0.3 else None
            _do_insert(tmp_db, model, pk, a, b, c)
            continue
        op(rng, tmp_db, model)
    _do_select_all_order_by_pk(tmp_db, model)
    _do_count_star(tmp_db, model)
    _do_group_by_b_sum_a(tmp_db, model)


def test_random_session_persists_across_reopen(tmp_db_path):
    """Random writes, close, reopen, verify the on-disk state survives.

    Catches the classic "in-memory state looks right but on-disk pages
    don't" bug. The test asserts that a fresh SELECT against the
    reopened DB returns exactly the same rows that the pre-close SELECT
    did — no further writes, no replay of the rng.

    Pattern (per project memory feedback, do not regress):
      1. Record an op list from a seeded rng, BEFORE touching the DB
         or any model. The recorder is state-independent — replaying
         the same list twice produces identical SQL.
      2. Replay once on a fresh DB (db1) + fresh model (model1).
      3. Capture db1's state via ``SELECT *`` and assert the model
         mirrors it (sanity: the recorder is correct).
      4. Close db1.
      5. Open db2 (loads from disk).
      6. Capture db2's state via ``SELECT *`` — NO replay.
      7. db1_dump must equal db2_dump.

    The previous version of this test replayed the rng (NOT a recorded
    op list) against the reopened DB, so INSERTs that had already
    succeeded in session 1 were re-issued in session 2 and raised
    ConstraintError on the DB. The model and DB diverged in ways that
    masked persistence bugs. Replaying the recorded op list is the only
    way to actually test close+reopen.
    """
    rng = random.Random(7)
    db1 = Database(str(tmp_db_path))
    _bootstrap(db1)
    model1 = ReferenceModel()
    ops = _record_ops(rng, 300)
    _replay_ops(ops, db1, model1)
    db1_dump = db1.execute("SELECT pk, a, b, c FROM t ORDER BY pk").rows
    db1.close()

    # Sanity: the in-memory model should mirror the DB state at close
    # time. If this fails, _record_ops or _replay_ops is broken — not
    # the database. (We never assert this against db2, because db2 is
    # a separate in-memory model that starts empty and is irrelevant
    # to the persistence property we actually want to test.)
    model_dump = _stringify_rows(
        sorted(model1._rows.values(), key=lambda r: r["pk"]),
        ("pk", "a", "b", "c"),
    )
    assert db1_dump == model_dump, (
        f"Recorded ops did not produce a model that matches the DB at "
        f"close time (recorder bug?):\n  db1:    {db1_dump}\n  model1: {model_dump}"
    )

    db2 = Database(str(tmp_db_path))
    db2_dump = db2.execute("SELECT pk, a, b, c FROM t ORDER BY pk").rows
    db2.close()

    assert db1_dump == db2_dump, (
        f"Reopened DB state differs from pre-close state:\n"
        f"  db1 (pre-close):  {db1_dump}\n"
        f"  db2 (post-reopen): {db2_dump}"
    )


def test_pk_collision_is_deterministic(tmp_db):
    """Inserting the same PK twice must always raise on both sides.

    Verifies the symmetric ``ConstraintError`` path for a focused
    scenario, separately from the fuzz loop.
    """
    _bootstrap(tmp_db)
    model = ReferenceModel()
    _do_insert(tmp_db, model, pk=1, a=10, b="a", c=1)
    with pytest.raises(ConstraintError):
        tmp_db.execute("INSERT INTO t VALUES (1, 20, 'b', 2)")
    # Model also rejects.
    with pytest.raises(ConstraintError):
        model.insert(1, 20, "b", 2)


def test_not_null_violation_is_deterministic(tmp_db):
    """INSERT with NULL into a NOT NULL column must raise on both sides."""
    _bootstrap(tmp_db)
    model = ReferenceModel()
    with pytest.raises(ConstraintError):
        tmp_db.execute("INSERT INTO t VALUES (1, NULL, 'b', 5)")
    with pytest.raises(ConstraintError):
        model.insert(1, None, "b", 5)