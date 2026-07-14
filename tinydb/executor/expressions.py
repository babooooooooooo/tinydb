"""Expression evaluator: AST → Value.

Operates on a Row's column namespace. Supports literals, column references,
arithmetic and comparison operators, boolean AND/OR/NOT, NULL semantics
(SQL 3-valued), and aggregate function calls are handled separately by the
Aggregate operator.
"""

from __future__ import annotations

from tinydb.errors import TypeMismatchError
from tinydb.executor.row import Row
from tinydb.parser.ast import (
    BinaryOp,
    ColumnRef,
    Expr,
    FunctionCall,
    Literal,
    Star,
    UnaryOp,
)
from tinydb.types import Tag, Value, UNKNOWN
from tinydb.types.check import coerce


def eval_expr(expr: Expr, row: Row) -> Value:
    """Evaluate ``expr`` against ``row``.

    Raises ``KeyError`` if a ColumnRef is not in the row's namespace.
    """
    if isinstance(expr, Literal):
        if expr.value is None:
            return Value.null()
        if isinstance(expr.value, bool):
            return Value.bool_(expr.value)
        if isinstance(expr.value, int):
            return Value.int_(expr.value)
        if isinstance(expr.value, float):
            return Value.float_(expr.value)
        if isinstance(expr.value, str):
            return Value.text(expr.value)
        raise TypeMismatchError(f"unsupported literal: {expr.value!r}")
    if isinstance(expr, ColumnRef):
        return row[expr.name]
    if isinstance(expr, Star):
        # Star is meaningful only in COUNT(*); return NULL as a sentinel.
        return Value.null()
    if isinstance(expr, UnaryOp):
        if expr.op == "NOT":
            inner = eval_expr(expr.operand, row)
            if inner.is_null:
                return Value.null()
            return Value.bool_(not bool(inner.payload))
        raise TypeMismatchError(f"unsupported unary op: {expr.op}")
    if isinstance(expr, BinaryOp):
        return _eval_binary(expr, row)
    if isinstance(expr, FunctionCall):
        # Aggregate functions are intercepted by Aggregate operator; in row-
        # level evaluation, only the bare value (or NULL for *) is needed.
        if expr.name == "COUNT":
            return Value.int_(1)
        return eval_expr(expr.arg, row)
    raise TypeMismatchError(f"unsupported expression: {type(expr).__name__}")


def _eval_binary(expr: BinaryOp, row: Row) -> Value:
    op = expr.op
    left = eval_expr(expr.left, row)
    right = eval_expr(expr.right, row)

    if op in ("AND", "OR"):
        return _eval_bool_op(op, left, right)
    if op in ("+", "-", "*", "/"):
        return _eval_arith(op, left, right)
    if op in ("=", "<>", "<", "<=", ">", ">="):
        return _eval_comparison(op, left, right)
    raise TypeMismatchError(f"unsupported binary op: {op}")


def _eval_bool_op(op: str, left: Value, right: Value) -> Value:
    # SQL 3-valued boolean logic.
    def to_bool(v: Value) -> bool | None:
        if v.is_null:
            return None
        return bool(v.payload)

    lb, rb = to_bool(left), to_bool(right)
    if op == "AND":
        if lb is False or rb is False:
            return Value.bool_(False)
        if lb is None or rb is None:
            return Value.null()
        return Value.bool_(True)
    if op == "OR":
        if lb is True or rb is True:
            return Value.bool_(True)
        if lb is None or rb is None:
            return Value.null()
        return Value.bool_(False)
    raise TypeMismatchError(f"unsupported bool op: {op}")


def _eval_arith(op: str, left: Value, right: Value) -> Value:
    if left.is_null or right.is_null:
        return Value.null()
    if left.tag not in (Tag.INT, Tag.FLOAT) or right.tag not in (Tag.INT, Tag.FLOAT):
        raise TypeMismatchError(f"arithmetic on non-numeric: {left.tag} {op} {right.tag}")
    # INT-INT arithmetic stays in pure-int domain so values beyond 2^53
    # (which float can't represent exactly) keep full Python precision.
    if left.tag is Tag.INT and right.tag is Tag.INT and op != "/":
        a, b = int(left.payload), int(right.payload)
        if op == "+":
            return Value.int_(a + b)
        if op == "-":
            return Value.int_(a - b)
        if op == "*":
            return Value.int_(a * b)
        raise TypeMismatchError(f"unsupported arith op: {op}")
    # Mixed INT/FLOAT or division: float domain.
    a, b = float(left.payload), float(right.payload)
    if op == "+":
        result = a + b
    elif op == "-":
        result = a - b
    elif op == "*":
        result = a * b
    elif op == "/":
        result = a / b
    else:
        raise TypeMismatchError(f"unsupported arith op: {op}")
    return Value.float_(result)


def _eval_comparison(op: str, left: Value, right: Value) -> Value:
    # NULL propagates as UNKNOWN (= Python None).
    if left.is_null or right.is_null:
        return Value.null()
    if left.tag is right.tag:
        if op == "=":
            return Value.bool_(left.payload == right.payload)
        if op == "<>":
            return Value.bool_(left.payload != right.payload)
        if op == "<":
            return Value.bool_(left.payload < right.payload)
        if op == "<=":
            return Value.bool_(left.payload <= right.payload)
        if op == ">":
            return Value.bool_(left.payload > right.payload)
        if op == ">=":
            return Value.bool_(left.payload >= right.payload)
    # Numeric promotion.
    if left.tag in (Tag.INT, Tag.FLOAT) and right.tag in (Tag.INT, Tag.FLOAT):
        a, b = float(left.payload), float(right.payload)
        if op == "=":
            return Value.bool_(a == b)
        if op == "<>":
            return Value.bool_(a != b)
        if op == "<":
            return Value.bool_(a < b)
        if op == "<=":
            return Value.bool_(a <= b)
        if op == ">":
            return Value.bool_(a > b)
        if op == ">=":
            return Value.bool_(a >= b)
    return Value.null()  # incomparable types → UNKNOWN


def is_truthy(value: Value) -> bool | None:
    """Coerce a Value to a Python bool, propagating NULL as None.

    Used by Filter: rows with NULL predicates are excluded (consistent
    with the WHERE clause's role of selecting rows, not asserting truth).
    """
    if value.is_null:
        return None
    return bool(value.payload)