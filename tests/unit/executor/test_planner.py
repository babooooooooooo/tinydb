"""Tests for the planner: AST → operator tree."""

from __future__ import annotations

import pytest

from tinydb import Database
from tinydb.executor.aggregate import Aggregate
from tinydb.executor.filter import Filter, Project, Sort
from tinydb.executor.operator import Operator
from tinydb.executor.planner import plan
from tinydb.executor.scan import IndexScan, SeqScan
from tinydb.parser.parser import parse


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


def test_select_falls_back_to_seqscan_for_range(tmp_db):
    tmp_db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
    stmts = parse("SELECT * FROM t WHERE id > 5")
    op = plan(stmts[0], tmp_db.catalog, tmp_db.pool, tmp_db.freelist)
    assert isinstance(_find_scan(op), SeqScan)


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
