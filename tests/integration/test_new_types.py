"""End-to-end integration tests for the SQL92 type extensions (C11).

These tests exercise the full pipeline (parser -> planner -> executor ->
disk -> renderer) for the 8 new scalar types plus the alias keywords added
in C1-C10. They are not unit tests: no in-memory fixtures, no mocks. Each
test opens a real file-backed database, issues real SQL, and inspects the
rendered ResultSet output.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from tinydb import Database
from tinydb.errors import TypeMismatchError


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "new_types.db"


class TestAllNewTypesRoundTrip:
    """Single-row table containing every new type + alias. The rendered
    SELECT row must exactly match the inserted values, with correct CHAR
    padding and DATE/TIMESTAMP ISO formatting.
    """

    def test_create_insert_select_one_row(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute(
                "CREATE TABLE demo ("
                "  id BIGINT PRIMARY KEY,"
                "  name VARCHAR(20),"
                "  code CHAR(3),"
                "  d DATE,"
                "  tm TIME,"
                "  ts TIMESTAMP,"
                "  price DECIMAL(10, 2),"
                "  n SMALLINT,"
                "  f FLOAT,"
                "  x DOUBLE,"
                "  y REAL,"
                "  b BOOLEAN"
                ")"
            )
            db.execute(
                "INSERT INTO demo VALUES ("
                "  1,"
                "  'alice',"
                "  'AB',"
                "  '2025-01-15',"
                "  '13:45:30',"
                "  '2025-01-15T13:45:30',"
                "  '3.14',"
                "  42,"
                "  1.5,"
                "  2.5,"
                "  3.5,"
                "  TRUE"
                ")"
            )
            rs = db.execute("SELECT * FROM demo")
            row = rs.rows[0]
            assert row[0] == "1"
            assert row[1] == "alice"
            assert row[2] == "AB "  # CHAR(3) right-padded
            assert row[3] == "2025-01-15"
            assert row[4] == "13:45:30"
            assert row[5] == "2025-01-15T13:45:30+00:00"
            assert row[6] == "3.14"
            assert row[7] == "42"
            assert row[8] == "1.5"
            assert row[9] == "2.5"
            assert row[10] == "3.5"
            assert row[11] == "TRUE"


class TestAliasColumns:
    """DOUBLE/REAL/BOOLEAN are syntactic aliases for FLOAT/BOOL and must
    behave identically at storage + render time.
    """

    def test_double_persists_as_float(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute("CREATE TABLE t (id INT PRIMARY KEY, x DOUBLE, y REAL, b BOOLEAN)")
            db.execute("INSERT INTO t VALUES (1, 1.5, 2.5, TRUE)")
            rs = db.execute("SELECT x, y, b FROM t")
            row = rs.rows[0]
            assert row[0] == "1.5"
            assert row[1] == "2.5"
            assert row[2] == "TRUE"


class TestBoundaryRejections:
    """Bad values must raise TypeMismatchError at INSERT/UPDATE time —
    they must NEVER land on disk."""

    def test_varchar_too_long_raises(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute("CREATE TABLE t (s VARCHAR(3))")
            with pytest.raises(TypeMismatchError):
                db.execute("INSERT INTO t VALUES ('abcdef')")
            # Bad row must not have been written.
            rs = db.execute("SELECT * FROM t")
            assert rs.rows == []

    def test_char_too_long_raises(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute("CREATE TABLE t (s CHAR(3))")
            with pytest.raises(TypeMismatchError):
                db.execute("INSERT INTO t VALUES ('abcdef')")
            rs = db.execute("SELECT * FROM t")
            assert rs.rows == []

    def test_smallint_overflow_raises(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute("CREATE TABLE t (n SMALLINT)")
            with pytest.raises(TypeMismatchError):
                db.execute("INSERT INTO t VALUES (99999)")
            rs = db.execute("SELECT * FROM t")
            assert rs.rows == []

    def test_smallint_underflow_raises(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute("CREATE TABLE t (n SMALLINT)")
            with pytest.raises(TypeMismatchError):
                db.execute("INSERT INTO t VALUES (-99999)")
            rs = db.execute("SELECT * FROM t")
            assert rs.rows == []

    def test_bigint_overflow_raises(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute("CREATE TABLE t (n BIGINT)")
            with pytest.raises(TypeMismatchError):
                # 2^63 does not fit; 2^63-1 is the max.
                db.execute(f"INSERT INTO t VALUES ({2**63})")
            rs = db.execute("SELECT * FROM t")
            assert rs.rows == []

    def test_date_invalid_format_raises(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute("CREATE TABLE t (d DATE)")
            with pytest.raises(TypeMismatchError):
                db.execute("INSERT INTO t VALUES ('yesterday')")
            rs = db.execute("SELECT * FROM t")
            assert rs.rows == []

    def test_time_invalid_format_raises(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute("CREATE TABLE t (tm TIME)")
            with pytest.raises(TypeMismatchError):
                db.execute("INSERT INTO t VALUES ('25:00:00')")
            rs = db.execute("SELECT * FROM t")
            assert rs.rows == []

    def test_decimal_overflow_raises(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute("CREATE TABLE t (p DECIMAL(4, 2))")
            with pytest.raises(TypeMismatchError):
                # 12345 has 5 integer digits, exceeds DECIMAL(4, 2).
                db.execute("INSERT INTO t VALUES ('12345')")
            rs = db.execute("SELECT * FROM t")
            assert rs.rows == []

    def test_decimal_invalid_format_raises(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute("CREATE TABLE t (p DECIMAL(4, 2))")
            with pytest.raises(TypeMismatchError):
                db.execute("INSERT INTO t VALUES ('abc')")
            rs = db.execute("SELECT * FROM t")
            assert rs.rows == []


class TestFLOATRejectInfNaN:
    """FLOAT must reject inf/NaN. The SQL lexer cannot produce these from
    literal tokens, so exercise the boundary by constructing a Value and
    coercing it directly.
    """

    def test_inf_rejected(self) -> None:
        from tinydb.types import Tag, Value
        from tinydb.types.check import coerce

        with pytest.raises(TypeMismatchError):
            coerce(Value.float_(math.inf), Tag.FLOAT)

    def test_neg_inf_rejected(self) -> None:
        from tinydb.types import Tag, Value
        from tinydb.types.check import coerce

        with pytest.raises(TypeMismatchError):
            coerce(Value.float_(-math.inf), Tag.FLOAT)

    def test_nan_rejected(self) -> None:
        from tinydb.types import Tag, Value
        from tinydb.types.check import coerce

        with pytest.raises(TypeMismatchError):
            coerce(Value.float_(math.nan), Tag.FLOAT)


class TestPersistenceAcrossReopen:
    """The new on-disk formats (VERSION=2) must survive a close/reopen cycle
    for every new tag.
    """

    def test_new_types_persist_across_reopen(self, db_path: Path) -> None:
        # Session 1: create + insert.
        with Database(str(db_path)) as db:
            db.execute(
                "CREATE TABLE log ("
                "  id BIGINT PRIMARY KEY,"
                "  msg VARCHAR(50),"
                "  code CHAR(4),"
                "  d DATE,"
                "  ts TIMESTAMP"
                ")"
            )
            db.execute(
                "INSERT INTO log VALUES ("
                "  1, 'hello', 'ABCD', '2025-01-15', '2025-01-15T13:45:30'"
                ")"
            )

        # Session 2: reopen + verify.
        with Database(str(db_path)) as db:
            rs = db.execute("SELECT * FROM log")
            assert rs.rows == [[
                "1",
                "hello",
                "ABCD",  # CHAR(4) at full length
                "2025-01-15",
                "2025-01-15T13:45:30+00:00",
            ]]


class TestNullIntoNewTypes:
    """NULL must be accepted by all new types (no payload validation)."""

    def test_null_inserts_into_each_new_type(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute(
                "CREATE TABLE t ("
                "  id INT PRIMARY KEY,"
                "  v VARCHAR(10),"
                "  c CHAR(3),"
                "  d DATE,"
                "  tm TIME,"
                "  ts TIMESTAMP,"
                "  p DECIMAL(10, 2),"
                "  s SMALLINT,"
                "  b BIGINT"
                ")"
            )
            db.execute(
                "INSERT INTO t VALUES ("
                "  1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL"
                ")"
            )
            rs = db.execute("SELECT * FROM t")
            assert rs.rows == [["1", "", "", "", "", "", "", "", ""]]


class TestBoundaryAccepted:
    """Acceptance side of the boundary tests: edge values at the limit
    must succeed.
    """

    def test_smallint_max_ok(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute("CREATE TABLE t (n SMALLINT)")
            db.execute("INSERT INTO t VALUES (32767)")
            assert db.execute("SELECT * FROM t").rows == [["32767"]]

    def test_smallint_min_ok(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute("CREATE TABLE t (n SMALLINT)")
            db.execute("INSERT INTO t VALUES (-32768)")
            assert db.execute("SELECT * FROM t").rows == [["-32768"]]

    def test_bigint_max_ok(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute("CREATE TABLE t (n BIGINT)")
            db.execute(f"INSERT INTO t VALUES ({2**63 - 1})")
            assert db.execute("SELECT * FROM t").rows == [[str(2**63 - 1)]]

    def test_bigint_min_ok(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute("CREATE TABLE t (n BIGINT)")
            db.execute(f"INSERT INTO t VALUES ({-2**63})")
            assert db.execute("SELECT * FROM t").rows == [[str(-2**63)]]

    def test_varchar_at_exact_length_ok(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute("CREATE TABLE t (s VARCHAR(3))")
            db.execute("INSERT INTO t VALUES ('abc')")
            assert db.execute("SELECT * FROM t").rows == [["abc"]]

    def test_char_at_exact_length_ok(self, db_path: Path) -> None:
        with Database(str(db_path)) as db:
            db.execute("CREATE TABLE t (s CHAR(3))")
            db.execute("INSERT INTO t VALUES ('abc')")
            assert db.execute("SELECT * FROM t").rows == [["abc"]]
