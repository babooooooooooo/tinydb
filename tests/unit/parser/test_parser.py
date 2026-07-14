"""Tests for the recursive-descent SQL parser."""

from __future__ import annotations

import pytest

from tinydb.errors import ParseError
from tinydb.parser.ast import (
    Assignment,
    BeginStmt,
    BinaryOp,
    ColumnDef,
    ColumnRef,
    CommitStmt,
    CreateTableStmt,
    DeleteStmt,
    DropTableStmt,
    FunctionCall,
    InsertStmt,
    Literal,
    OrderItem,
    RollbackStmt,
    SelectItem,
    SelectStmt,
    Star,
    UnaryOp,
    UpdateStmt,
)
from tinydb.parser.parser import parse
from tinydb.types import Tag


def _parse_one(sql: str):
    stmts = parse(sql)
    assert len(stmts) == 1, f"expected 1 stmt, got {len(stmts)}"
    return stmts[0]


class TestCreateTable:
    def test_single_column(self):
        stmt = _parse_one("CREATE TABLE t (id INT)")
        assert isinstance(stmt, CreateTableStmt)
        assert stmt.name == "t"
        assert len(stmt.columns) == 1
        c = stmt.columns[0]
        assert c.name == "id" and c.type is Tag.INT

    def test_multiple_columns_with_constraints(self):
        stmt = _parse_one(
            "CREATE TABLE users ("
            "  id INT PRIMARY KEY,"
            "  email TEXT UNIQUE NOT NULL,"
            "  age INT"
            ")"
        )
        assert stmt.name == "users"
        id_c, email_c, age_c = stmt.columns
        assert id_c.primary_key and not id_c.not_null
        assert email_c.unique and email_c.not_null
        assert not age_c.unique and not age_c.not_null

    def test_duplicate_column_raises(self):
        with pytest.raises(ParseError):
            parse("CREATE TABLE t (id INT, id TEXT)")

    def test_unsupported_type_raises(self):
        with pytest.raises(ParseError):
            parse("CREATE TABLE t (data BLOB)")


class TestInsert:
    def test_no_column_list(self):
        stmt = _parse_one("INSERT INTO users VALUES (1, 'alice', true)")
        assert isinstance(stmt, InsertStmt)
        assert stmt.table == "users"
        assert stmt.columns is None
        assert len(stmt.values) == 3

    def test_with_column_list(self):
        stmt = _parse_one("INSERT INTO users (name, age) VALUES ('bob', 30)")
        assert stmt.columns == ("name", "age")
        assert len(stmt.values) == 2


class TestSelect:
    def test_select_star(self):
        stmt = _parse_one("SELECT * FROM users")
        assert isinstance(stmt, SelectStmt)
        assert stmt.items[0].expr == Star()
        assert stmt.from_table == "users"

    def test_select_with_where(self):
        stmt = _parse_one("SELECT * FROM users WHERE age >= 18")
        assert stmt.where is not None
        assert isinstance(stmt.where, BinaryOp)
        assert stmt.where.op == ">="

    def test_select_with_group_order_limit(self):
        stmt = _parse_one(
            "SELECT city, COUNT(*) FROM users "
            "GROUP BY city ORDER BY COUNT(*) DESC LIMIT 10 OFFSET 5"
        )
        assert stmt.group_by == ("city",)
        assert stmt.order_by[0].desc
        assert stmt.limit == 10
        assert stmt.offset == 5

    def test_distinct(self):
        stmt = _parse_one("SELECT DISTINCT city FROM users")
        assert stmt.distinct is True

    def test_and_or_precedence(self):
        # `a OR b AND c` must parse as `a OR (b AND c)`.
        stmt = _parse_one("SELECT * FROM t WHERE a = 1 OR b = 2 AND c = 3")
        w = stmt.where
        assert isinstance(w, BinaryOp) and w.op == "OR"
        assert isinstance(w.right, BinaryOp) and w.right.op == "AND"

    def test_function_call(self):
        stmt = _parse_one("SELECT COUNT(*), SUM(age), AVG(salary) FROM users")
        assert isinstance(stmt.items[0].expr, FunctionCall)
        assert stmt.items[0].expr.name == "COUNT"
        assert stmt.items[1].expr.name == "SUM"
        assert stmt.items[2].expr.name == "AVG"


class TestUpdate:
    def test_update_with_where(self):
        stmt = _parse_one("UPDATE users SET age = 31 WHERE id = 1")
        assert isinstance(stmt, UpdateStmt)
        assert len(stmt.assignments) == 1
        assert stmt.assignments[0].column == "age"

    def test_update_multiple(self):
        stmt = _parse_one("UPDATE users SET a = 1, b = 2, c = 3")
        assert len(stmt.assignments) == 3


class TestDelete:
    def test_delete_with_where(self):
        stmt = _parse_one("DELETE FROM users WHERE age < 0")
        assert isinstance(stmt, DeleteStmt)
        assert stmt.table == "users"

    def test_delete_all(self):
        stmt = _parse_one("DELETE FROM users")
        assert stmt.where is None


class TestDropTable:
    def test_drop(self):
        stmt = _parse_one("DROP TABLE users")
        assert isinstance(stmt, DropTableStmt)
        assert stmt.name == "users"


class TestTransactionStatements:
    def test_begin(self):
        assert isinstance(_parse_one("BEGIN"), BeginStmt)

    def test_commit(self):
        assert isinstance(_parse_one("COMMIT"), CommitStmt)

    def test_rollback(self):
        assert isinstance(_parse_one("ROLLBACK"), RollbackStmt)


class TestMultipleStatements:
    def test_semicolon_separated(self):
        stmts = parse("CREATE TABLE t (x INT); INSERT INTO t VALUES (1); SELECT * FROM t")
        assert len(stmts) == 3
        assert isinstance(stmts[0], CreateTableStmt)
        assert isinstance(stmts[1], InsertStmt)
        assert isinstance(stmts[2], SelectStmt)

    def test_trailing_semicolon_ok(self):
        stmts = parse("BEGIN; COMMIT;")
        assert len(stmts) == 2


class TestExpressions:
    def test_negation(self):
        stmt = _parse_one("SELECT * FROM t WHERE x = -5")
        # WHERE is `x = -5`; the -5 is encoded as `0 - 5`.
        assert isinstance(stmt.where, BinaryOp)
        assert stmt.where.op == "="
        assert isinstance(stmt.where.right, BinaryOp)
        assert stmt.where.right.op == "-"
        assert stmt.where.right.left == Literal(0)

    def test_not(self):
        stmt = _parse_one("SELECT * FROM t WHERE NOT active")
        assert isinstance(stmt.where, UnaryOp)
        assert stmt.where.op == "NOT"

    def test_qualified_column(self):
        stmt = _parse_one("SELECT u.id FROM users u")
        assert stmt.from_table == "users"
        assert isinstance(stmt.items[0].expr, ColumnRef)
        assert stmt.items[0].expr.table == "u"
        assert stmt.items[0].expr.name == "id"