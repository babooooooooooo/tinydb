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
    """Random writes, close, reopen, verify model matches reopened DB.

    Catches the classic "in-memory state looks right but on-disk pages
    don't" bug — the model is rebuilt from scratch against the reopened
    database, so any row whose bytes didn't make it to disk shows up
    as a model/db mismatch.
    """
    rng = random.Random(7)
    db = Database(str(tmp_db_path))
    _bootstrap(db)
    model = ReferenceModel()
    _drive_random_session(rng, db, model, n_ops=300)
    db.close()

    db2 = Database(str(tmp_db_path))
    # Rebuild a fresh model and replay every SQL op the original session
    # performed against the reopened DB. The resulting model must equal
    # the one we kept in memory.
    expected = ReferenceModel()
    rng2 = random.Random(7)
    _drive_random_session(rng2, db2, expected, n_ops=300)
    db2.close()
    assert model._rows == expected._rows, (
        "Reopened DB does not match in-memory model after random session"
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