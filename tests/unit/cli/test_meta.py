"""Tests for meta-commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from tinydb import Database
from tinydb.cli.meta import get_handlers


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(str(tmp_path / "meta.db"))
    d.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT NOT NULL)")
    d.execute("INSERT INTO t VALUES (1, 'alice')")
    d.execute("INSERT INTO t VALUES (2, 'bob')")
    yield d
    d.close()


class TestTables:
    def test_lists_table_names(self, db, capsys):
        handlers = get_handlers()
        cont, out = handlers[".tables"](db, "")
        assert cont is True
        assert "t" in out

    def test_no_tables_says_so(self, tmp_path):
        d = Database(str(tmp_path / "empty.db"))
        try:
            handlers = get_handlers()
            cont, out = handlers[".tables"](d, "")
            assert cont is True
            assert "no tables" in out.lower()
        finally:
            d.close()


class TestSchema:
    def test_shows_create_table(self, db):
        handlers = get_handlers()
        cont, out = handlers[".schema"](db, "t")
        assert cont is True
        assert "CREATE TABLE t" in out
        assert "id" in out
        assert "name" in out
        assert "PRIMARY KEY" in out
        assert "NOT NULL" in out

    def test_unknown_table_reports_error(self, db):
        handlers = get_handlers()
        cont, out = handlers[".schema"](db, "missing")
        assert cont is True
        assert "error" in out.lower()

    def test_missing_arg_shows_usage(self, db):
        handlers = get_handlers()
        cont, out = handlers[".schema"](db, "")
        assert cont is True
        assert "usage" in out.lower()


class TestExit:
    def test_exit_returns_continue_false(self, db):
        handlers = get_handlers()
        cont, _out = handlers[".exit"](db, "")
        assert cont is False

    def test_quit_returns_continue_false(self, db):
        handlers = get_handlers()
        cont, _out = handlers[".quit"](db, "")
        assert cont is False


class TestHelp:
    def test_help_lists_commands(self, db):
        handlers = get_handlers()
        cont, out = handlers[".help"](db, "")
        assert cont is True
        assert ".tables" in out
        assert ".schema" in out
        assert ".exit" in out