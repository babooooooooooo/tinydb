"""Tests for the SQL lexer."""

from __future__ import annotations

import pytest

from tinydb.errors import ParseError
from tinydb.parser.lexer import Lexer, TokKind


def _toks(sql: str):
    return [(t.kind, t.lexeme) for t in Lexer(sql).tokenize() if t.kind is not TokKind.EOF]


class TestKeywords:
    def test_keyword_lowercase(self):
        kinds = _toks("select")
        assert kinds == [(TokKind.KEYWORD, "SELECT")]

    def test_keyword_uppercase(self):
        kinds = _toks("SELECT")
        assert kinds == [(TokKind.KEYWORD, "SELECT")]

    def test_keyword_mixed_case(self):
        kinds = _toks("SeLeCt")
        assert kinds == [(TokKind.KEYWORD, "SELECT")]


class TestIdentifiers:
    def test_ident(self):
        assert _toks("users") == [(TokKind.IDENT, "users")]

    def test_ident_with_digits(self):
        assert _toks("user_123") == [(TokKind.IDENT, "user_123")]


class TestLiterals:
    def test_int(self):
        assert _toks("42") == [(TokKind.INT, "42")]

    def test_float(self):
        assert _toks("3.14") == [(TokKind.FLOAT, "3.14")]

    def test_float_exponent_only(self):
        assert _toks("1e5") == [(TokKind.FLOAT, "1e5")]

    def test_float_trailing_dot(self):
        assert _toks("1.") == [(TokKind.FLOAT, "1.")]

    def test_float_leading_dot_with_exponent(self):
        assert _toks(".5e10") == [(TokKind.FLOAT, ".5e10")]

    def test_float_leading_dot(self):
        assert _toks(".5") == [(TokKind.FLOAT, ".5")]

    def test_float_with_exponent(self):
        assert _toks("1.5e10") == [(TokKind.FLOAT, "1.5e10")]

    def test_float_trailing_dot_with_exponent(self):
        assert _toks("1.e10") == [(TokKind.FLOAT, "1.e10")]

    def test_exponent_uppercase(self):
        assert _toks("1E5") == [(TokKind.FLOAT, "1E5")]

    def test_exponent_with_sign(self):
        assert _toks("1.5e-10") == [(TokKind.FLOAT, "1.5e-10")]
        assert _toks("1.5e+10") == [(TokKind.FLOAT, "1.5e+10")]

    def test_float_stops_at_first_non_ident(self):
        # The float match must stop at the first non-ident character so
        # `1.5+2.5` is FLOAT OP FLOAT, not one swallowed token.
        assert _toks("1.5+2.5") == [
            (TokKind.FLOAT, "1.5"),
            (TokKind.OP, "+"),
            (TokKind.FLOAT, "2.5"),
        ]

    def test_negative_float_via_minus_op(self):
        # SQL has no negative-number literal; `-1.5` is OP(-), FLOAT(1.5).
        assert _toks("-1.5") == [
            (TokKind.OP, "-"),
            (TokKind.FLOAT, "1.5"),
        ]

    def test_many_floats_in_one_statement(self):
        kinds = _toks("SELECT 1e5, 1., .5e10 FROM t")
        assert kinds == [
            (TokKind.KEYWORD, "SELECT"),
            (TokKind.FLOAT, "1e5"),
            (TokKind.COMMA, ","),
            (TokKind.FLOAT, "1."),
            (TokKind.COMMA, ","),
            (TokKind.FLOAT, ".5e10"),
            (TokKind.KEYWORD, "FROM"),
            (TokKind.IDENT, "t"),
        ]

    def test_string(self):
        assert _toks("'hello'") == [(TokKind.STRING, "hello")]

    def test_string_escaped_quote(self):
        assert _toks("'it''s'") == [(TokKind.STRING, "it's")]

    def test_unterminated_string_raises(self):
        with pytest.raises(ParseError):
            Lexer("'abc").tokenize()

    def test_bool_true(self):
        assert _toks("TRUE") == [(TokKind.BOOL, "TRUE")]

    def test_bool_false(self):
        assert _toks("false") == [(TokKind.BOOL, "FALSE")]

    def test_null(self):
        assert _toks("null") == [(TokKind.NULL, "NULL")]


class TestOperatorsAndPunct:
    def test_paren_comma_semi_star(self):
        assert _toks("(,);*") == [
            (TokKind.LPAREN, "("),
            (TokKind.COMMA, ","),
            (TokKind.RPAREN, ")"),
            (TokKind.SEMI, ";"),
            (TokKind.STAR, "*"),
        ]

    def test_two_char_ops(self):
        for op in ("<=", ">=", "<>", "!="):
            assert _toks(op) == [(TokKind.OP, op)]

    def test_single_char_ops(self):
        # ``*`` is emitted as STAR, not OP, so it's tested separately above.
        for op in ("+", "-", "/", "<", ">", "="):
            assert _toks(op) == [(TokKind.OP, op)]


class TestSkipping:
    def test_whitespace_skipped(self):
        assert _toks("  select  *  ") == [
            (TokKind.KEYWORD, "SELECT"),
            (TokKind.STAR, "*"),
        ]

    def test_line_comment_skipped(self):
        sql = "SELECT * -- pick all\nFROM t"
        assert _toks(sql) == [
            (TokKind.KEYWORD, "SELECT"),
            (TokKind.STAR, "*"),
            (TokKind.KEYWORD, "FROM"),
            (TokKind.IDENT, "t"),
        ]

    def test_unknown_character_raises(self):
        with pytest.raises(ParseError):
            Lexer("SELECT @ FROM t").tokenize()