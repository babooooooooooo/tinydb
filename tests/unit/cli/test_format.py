"""Tests for the tabular result formatter."""

from __future__ import annotations

from tinydb.cli.format import format_rows, format_rows_affected


class TestFormatRows:
    def test_empty_result_set(self):
        assert "empty" in format_rows([], []).lower()

    def test_single_column(self):
        out = format_rows(["x"], [["1"], ["2"]])
        assert "x" in out
        assert "1" in out
        assert "2" in out

    def test_multi_column_alignment(self):
        out = format_rows(["id", "name"], [["1", "alice"], ["2", "bob"]])
        lines = out.splitlines()
        assert "id" in lines[1]
        assert "name" in lines[1]
        assert "(2 rows)" in out

    def test_singular_row_count(self):
        out = format_rows(["id"], [["1"]])
        assert "(1 row)" in out

    def test_long_value_extends_column_width(self):
        out = format_rows(["name"], [["short"], ["a-much-longer-value"]])
        assert "a-much-longer-value" in out
        assert "name" in out

    def test_returns_string_ending_in_newline(self):
        out = format_rows(["a"], [["x"]])
        assert out.endswith("\n")


class TestFormatRowsAffected:
    def test_singular(self):
        assert "1 row affected" in format_rows_affected(1)

    def test_plural(self):
        assert "5 rows affected" in format_rows_affected(5)