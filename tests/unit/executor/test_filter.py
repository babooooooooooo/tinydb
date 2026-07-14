"""Tests for Filter, Project, Sort, Limit, Offset operators (via SELECT)."""

from __future__ import annotations

import pytest

from tinydb import Database


@pytest.fixture
def people(tmp_db):
    tmp_db.execute("CREATE TABLE p (id INT PRIMARY KEY, name TEXT NOT NULL, age INT)")
    rows = [
        (1, "alice", 30),
        (2, "bob", 25),
        (3, "carol", 35),
        (4, "dave", 22),
        (5, "eve", 28),
    ]
    for r in rows:
        tmp_db.execute(f"INSERT INTO p VALUES ({r[0]}, '{r[1]}', {r[2]})")
    return tmp_db


def test_filter_equality(people):
    rs = people.execute("SELECT name FROM p WHERE age = 30")
    assert rs.rows == [["alice"]]


def test_filter_inequality(people):
    rs = people.execute("SELECT name FROM p WHERE age <> 30")
    names = sorted(r[0] for r in rs.rows)
    assert names == ["bob", "carol", "dave", "eve"]


def test_filter_range(people):
    rs = people.execute("SELECT name FROM p WHERE age > 25 AND age < 35")
    names = sorted(r[0] for r in rs.rows)
    assert names == ["alice", "eve"]


def test_filter_or(people):
    rs = people.execute("SELECT name FROM p WHERE age = 22 OR age = 35")
    names = sorted(r[0] for r in rs.rows)
    assert names == ["carol", "dave"]


def test_project_specific_columns(people):
    rs = people.execute("SELECT name, age FROM p")
    assert rs.columns == ["name", "age"]
    assert len(rs.rows) == 5


def test_sort_ascending(people):
    rs = people.execute("SELECT name FROM p ORDER BY age")
    assert [r[0] for r in rs.rows] == ["dave", "bob", "eve", "alice", "carol"]


def test_sort_descending(people):
    rs = people.execute("SELECT name FROM p ORDER BY age DESC")
    assert [r[0] for r in rs.rows] == ["carol", "alice", "eve", "bob", "dave"]


def test_limit(people):
    rs = people.execute("SELECT name FROM p ORDER BY id LIMIT 2")
    assert [r[0] for r in rs.rows] == ["alice", "bob"]


def test_offset(people):
    rs = people.execute("SELECT name FROM p ORDER BY id OFFSET 2")
    assert [r[0] for r in rs.rows] == ["carol", "dave", "eve"]


def test_limit_offset_combined(people):
    rs = people.execute("SELECT name FROM p ORDER BY id LIMIT 2 OFFSET 1")
    assert [r[0] for r in rs.rows] == ["bob", "carol"]


def test_distinct(people):
    tmp_db = people
    tmp_db.execute("CREATE TABLE colors (name TEXT NOT NULL)")
    tmp_db.execute("INSERT INTO colors VALUES ('red')")
    tmp_db.execute("INSERT INTO colors VALUES ('blue')")
    tmp_db.execute("INSERT INTO colors VALUES ('red')")
    tmp_db.execute("INSERT INTO colors VALUES ('green')")
    rs = tmp_db.execute("SELECT DISTINCT name FROM colors ORDER BY name")
    assert [r[0] for r in rs.rows] == ["blue", "green", "red"]
