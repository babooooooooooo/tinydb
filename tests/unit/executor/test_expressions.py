"""Tests for the expression evaluator."""

from __future__ import annotations

import pytest

from tinydb.executor.expressions import eval_expr, is_truthy
from tinydb.executor.row import Row
from tinydb.parser.parser import parse
from tinydb.types import Value


def _row(items: dict[str, Value]) -> Row:
    return Row(items)


def _parse_expr(sql: str):
    # Pull the WHERE expression out of a SELECT.
    stmts = parse(f"SELECT 1 FROM t WHERE {sql}")
    return stmts[0].where


def test_literal_eval():
    expr = _parse_expr("1 = 1")
    r = _row({})
    v = eval_expr(expr, r)
    assert v.payload is True


def test_column_ref_eval():
    expr = _parse_expr("x > 5")
    r = _row({"x": Value.int_(10)})
    v = eval_expr(expr, r)
    assert v.payload is True


def test_arithmetic_eval():
    expr = _parse_expr("a + b = 3")
    r = _row({"a": Value.int_(1), "b": Value.int_(2)})
    v = eval_expr(expr, r)
    assert v.payload is True


def test_null_propagation_in_arithmetic():
    expr = _parse_expr("a + b = 3")
    r = _row({"a": Value.null(), "b": Value.int_(2)})
    v = eval_expr(expr, r)
    assert v.is_null


def test_null_propagation_in_comparison():
    expr = _parse_expr("a = b")
    r = _row({"a": Value.null(), "b": Value.int_(1)})
    v = eval_expr(expr, r)
    assert v.is_null


def test_three_valued_and_false():
    expr = _parse_expr("a = 1 AND b = 1")
    r = _row({"a": Value.int_(1), "b": Value.int_(2)})
    v = eval_expr(expr, r)
    assert v.payload is False


def test_three_valued_and_null():
    expr = _parse_expr("a = 1 AND b = 1")
    r = _row({"a": Value.int_(1), "b": Value.null()})
    v = eval_expr(expr, r)
    assert v.is_null


def test_three_valued_or_true():
    expr = _parse_expr("a = 1 OR b = 2")
    r = _row({"a": Value.int_(1), "b": Value.int_(99)})
    v = eval_expr(expr, r)
    assert v.payload is True


def test_not_eval():
    expr = _parse_expr("NOT a = 1")
    r = _row({"a": Value.int_(0)})
    v = eval_expr(expr, r)
    assert v.payload is True


def test_not_null_eval():
    expr = _parse_expr("NOT a = 1")
    r = _row({"a": Value.null()})
    v = eval_expr(expr, r)
    assert v.is_null


def test_is_truthy_helper():
    assert is_truthy(Value.bool_(True)) is True
    assert is_truthy(Value.bool_(False)) is False
    assert is_truthy(Value.null()) is None


def test_int_arith_preserves_precision_beyond_2_pow_53():
    """INT + INT must not round-trip through float.

    Python ints are arbitrary-precision, but converting to float and
    back loses precision once the magnitude exceeds 2^53. Regression:
    a - b where a = 10^18 + 7, b = 10^18 produced 0 instead of 7.
    """
    big = 10**18 + 7
    expr = _parse_expr("a - b = 7")
    r = _row({"a": Value.int_(big), "b": Value.int_(10**18)})
    v = eval_expr(expr, r)
    assert v.payload is True, (
        f"INT arithmetic lost precision: a-b should equal 7, "
        f"got a-b evaluated as {big - 10**18}"
    )


def test_int_unary_minus_preserves_precision():
    """Unary minus on a large INT must not lose precision via float cast.

    The parser rewrites -x as 0 - x; the resulting arithmetic must
    stay in the int domain when both operands are INT.
    """
    big = 10**18 + 13
    expr = _parse_expr("-a = -1000000000000000013")
    r = _row({"a": Value.int_(big)})
    v = eval_expr(expr, r)
    assert v.payload is True


def test_int_arith_with_large_values_returns_int():
    """INT arithmetic on big values must keep the result as INT, not FLOAT."""
    big = 10**18 + 5
    expr = _parse_expr("a + b")
    r = _row({"a": Value.int_(big), "b": Value.int_(1)})
    v = eval_expr(expr, r)
    assert v.tag.value == 0, f"expected INT, got tag {v.tag} payload {v.payload}"
    assert v.payload == big + 1
