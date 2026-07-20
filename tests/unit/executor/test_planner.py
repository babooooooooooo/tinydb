"""Tests for the planner: AST → operator tree."""

from __future__ import annotations

import pytest

from tinydb import Database
from tinydb.executor.aggregate import Aggregate
from tinydb.executor.filter import Filter, Project, Sort
from tinydb.executor.operator import Operator
from tinydb.executor.planner import IndexBound, _extract_index_bound, plan
from tinydb.executor.scan import IndexScan, SeqScan
from tinydb.parser.parser import parse
from tinydb.types import Tag


def _find_scan(op: Operator) -> Operator:
    """Walk the operator tree and return the leftmost scan operator."""
    cur = op
    while hasattr(cur, "child"):
        cur = cur.child
    return cur


def test_select_plans_seqscan_without_index(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT, name TEXT)")
    stmts = parse("SELECT * FROM t")
    op = plan(stmts[0], tmp_db.catalog, tmp_db.pool, tmp_db.freelist)
    assert isinstance(_find_scan(op), SeqScan)


def test_select_plans_indexscan_for_pk_equality(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
    stmts = parse("SELECT * FROM t WHERE id = 5")
    op = plan(stmts[0], tmp_db.catalog, tmp_db.pool, tmp_db.freelist)
    assert isinstance(_find_scan(op), IndexScan)


def test_select_plans_indexscan_for_unique_equality(tmp_db):
    tmp_db.execute("CREATE TABLE t (email TEXT UNIQUE, name TEXT)")
    stmts = parse("SELECT * FROM t WHERE email = 'a@x.com'")
    op = plan(stmts[0], tmp_db.catalog, tmp_db.pool, tmp_db.freelist)
    assert isinstance(_find_scan(op), IndexScan)


def test_select_picks_indexscan_for_pk_range(tmp_db):
    # Range predicates on a PK now use IndexScan with a bound; SeqScan
    # is only used when no index-bound can be extracted.
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
    stmts = parse("SELECT * FROM t WHERE id > 5")
    op = plan(stmts[0], tmp_db.catalog, tmp_db.pool, tmp_db.freelist)
    scan = _find_scan(op)
    assert isinstance(scan, IndexScan)
    assert scan.bound is not None
    assert scan.bound.column == "id"
    assert scan.bound.low is not None and scan.bound.low.payload == 5
    assert not scan.bound.low_inclusive


def test_select_falls_back_to_seqscan_for_unindexed_column(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
    stmts = parse("SELECT * FROM t WHERE name = 'alice'")
    op = plan(stmts[0], tmp_db.catalog, tmp_db.pool, tmp_db.freelist)
    assert isinstance(_find_scan(op), SeqScan)


def test_select_with_conjunction_picks_index(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, age INT)")
    stmts = parse("SELECT * FROM t WHERE id = 1 AND age = 30")
    op = plan(stmts[0], tmp_db.catalog, tmp_db.pool, tmp_db.freelist)
    assert isinstance(_find_scan(op), IndexScan)


def test_aggregate_wraps_aggregation(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT, region TEXT)")
    stmts = parse("SELECT region, COUNT(*) FROM t GROUP BY region")
    op = plan(stmts[0], tmp_db.catalog, tmp_db.pool, tmp_db.freelist)
    assert isinstance(op, Aggregate)


def test_filter_wrap_for_where(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT)")
    stmts = parse("SELECT * FROM t WHERE id > 5")
    op = plan(stmts[0], tmp_db.catalog, tmp_db.pool, tmp_db.freelist)
    # Filter is between Project and the scan.
    assert isinstance(_find_scan(op), SeqScan)
    # Walk up one level from the scan: must be a Filter.
    parent = op
    while parent is not None and not isinstance(parent, Filter):
        parent = getattr(parent, "child", None)
    assert isinstance(parent, Filter)


class TestExtractIndexBound:
    """Tests for planner._extract_index_bound: WHERE expr → IndexBound."""

    @staticmethod
    def _bound_for(query: str, tmp_db) -> IndexBound | None:
        stmts = parse(query)
        stmt = stmts[0]
        table = tmp_db.catalog.get_table(stmt.from_table)
        return _extract_index_bound(stmt.where, table)

    def test_extract_eq_predicate(self, tmp_db):
        tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
        bound = self._bound_for("SELECT * FROM t WHERE id = 5", tmp_db)
        assert isinstance(bound, IndexBound)
        assert bound.column == "id"
        assert bound.low is not None and bound.low.payload == 5
        assert bound.high is not None and bound.high.payload == 5
        assert bound.low_inclusive and bound.high_inclusive
        assert not bound.always_empty

    def test_extract_range_low(self, tmp_db):
        tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        bound = self._bound_for("SELECT * FROM t WHERE id > 5", tmp_db)
        assert isinstance(bound, IndexBound)
        assert bound.column == "id"
        assert bound.low is not None and bound.low.payload == 5
        assert not bound.low_inclusive
        assert bound.high is None

    def test_extract_range_high(self, tmp_db):
        tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        bound = self._bound_for("SELECT * FROM t WHERE id < 10", tmp_db)
        assert isinstance(bound, IndexBound)
        assert bound.column == "id"
        assert bound.low is None
        assert bound.high is not None and bound.high.payload == 10
        assert not bound.high_inclusive

    def test_extract_range_between(self, tmp_db):
        tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        bound = self._bound_for(
            "SELECT * FROM t WHERE id >= 5 AND id <= 10", tmp_db
        )
        assert isinstance(bound, IndexBound)
        assert bound.column == "id"
        assert bound.low.payload == 5 and bound.low_inclusive
        assert bound.high.payload == 10 and bound.high_inclusive
        assert not bound.always_empty

    def test_extract_conjunction_with_eq(self, tmp_db):
        tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, age INT)")
        bound = self._bound_for(
            "SELECT * FROM t WHERE id = 1 AND age = 30", tmp_db
        )
        assert isinstance(bound, IndexBound)
        assert bound.column == "id"
        assert bound.low.payload == 1
        assert bound.high.payload == 1

    def test_extract_or_returns_none(self, tmp_db):
        tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, age INT)")
        bound = self._bound_for(
            "SELECT * FROM t WHERE id = 1 OR age = 30", tmp_db
        )
        assert bound is None

    def test_extract_not_returns_none(self, tmp_db):
        tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        bound = self._bound_for(
            "SELECT * FROM t WHERE NOT id = 5", tmp_db
        )
        assert bound is None

    def test_extract_unindexed_column_returns_none(self, tmp_db):
        tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
        bound = self._bound_for(
            "SELECT * FROM t WHERE name = 'alice'", tmp_db
        )
        assert bound is None

    def test_extract_conflicting_bounds_marks_empty(self, tmp_db):
        tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        bound = self._bound_for(
            "SELECT * FROM t WHERE id > 10 AND id < 5", tmp_db
        )
        assert isinstance(bound, IndexBound)
        assert bound.always_empty

    def test_extract_coerces_text_literal(self, tmp_db):
        tmp_db.execute("CREATE TABLE t (email VARCHAR(50) UNIQUE)")
        bound = self._bound_for(
            "SELECT * FROM t WHERE email = 'a@x.com'", tmp_db
        )
        assert isinstance(bound, IndexBound)
        assert bound.column == "email"
        # TEXT literal should be coerced into the declared VARCHAR type.
        assert bound.low.tag is Tag.VARCHAR
        assert bound.low.payload == "a@x.com"
