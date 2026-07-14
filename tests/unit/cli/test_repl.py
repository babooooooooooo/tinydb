"""Tests for the REPL driver (statement buffer and execution)."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

from tinydb import Database
from tinydb.cli.repl import _is_complete, _split_statements, run_script


def test_is_complete_simple():
    assert _is_complete("SELECT 1;")


def test_is_complete_with_string_literal():
    assert _is_complete("INSERT INTO t VALUES ('a;b', 1);")


def test_is_complete_multiline():
    assert _is_complete("SELECT *\nFROM t\nWHERE id = 1;")


def test_is_complete_false_without_semicolon():
    assert not _is_complete("SELECT 1")


def test_is_complete_ignores_semicolon_in_string():
    assert not _is_complete("INSERT INTO t VALUES ('ab;cd'")


def test_split_statements_two_simple():
    parts = _split_statements("SELECT 1; SELECT 2;")
    assert parts == ["SELECT 1", "SELECT 2"]


def test_split_statements_handles_string_with_semicolon():
    parts = _split_statements("INSERT INTO t VALUES ('a;b', 1); SELECT 2;")
    assert parts == [
        "INSERT INTO t VALUES ('a;b', 1)",
        "SELECT 2",
    ]


def test_run_script_select(tmp_path: Path):
    p = str(tmp_path / "r.db")
    Database(p).close()
    out = StringIO()
    run_script(p, "CREATE TABLE t (id INT PRIMARY KEY, name TEXT); INSERT INTO t VALUES (1, 'alice'); SELECT * FROM t;", stdout=out)
    text = out.getvalue()
    assert "alice" in text
    assert "1 row" in text  # the CREATE and INSERT both report "1 row affected"


def test_run_script_meta_command(tmp_path: Path):
    p = str(tmp_path / "m.db")
    Database(p).close()
    out = StringIO()
    run_script(p, ".tables", stdout=out)
    text = out.getvalue()
    assert "no tables" in text.lower()


def test_run_script_error_continues(tmp_path: Path):
    p = str(tmp_path / "e.db")
    Database(p).close()
    out = StringIO()
    run_script(p, "SELECT * FROM missing; CREATE TABLE t (id INT);", stdout=out)
    text = out.getvalue()
    assert "error" in text.lower()