"""Filter, Project, Sort, Limit, Offset operators."""

from __future__ import annotations

from typing import Any

from tinydb.executor.expressions import eval_expr, is_truthy
from tinydb.executor.operator import Operator
from tinydb.executor.row import Row
from tinydb.parser.ast import Expr, OrderItem, SelectItem, Star
from tinydb.types import Tag, Value


class Filter(Operator):
    """Apply a predicate; emit only rows for which the predicate is TRUE.

    Rows where the predicate evaluates to NULL/FALSE/UNKNOWN are skipped,
    consistent with SQL WHERE semantics.
    """

    def __init__(self, child: Operator, predicate: Expr) -> None:
        self.child = child
        self.predicate = predicate

    def open(self) -> None:
        self.child.open()

    def close(self) -> None:
        self.child.close()

    def next(self) -> Row | None:
        while True:
            row = self.child.next()
            if row is None:
                return None
            v = eval_expr(self.predicate, row)
            t = is_truthy(v)
            if t is True:
                return row


class Project(Operator):
    """Project a fixed list of (expr, alias) output items.

    Each output row is a fresh Row keyed by alias (or the expression's
    textual form when no alias is provided).
    """

    def __init__(self, child: Operator, items: tuple[SelectItem, ...]) -> None:
        self.child = child
        self.items = items
        self._expanded_columns: list[tuple[Expr, str]] | None = None

    def open(self) -> None:
        self.child.open()
        # Pre-compute (expr, output_name) pairs; expand Star into column refs.
        self._expanded_columns = []
        for item in self.items:
            if isinstance(item.expr, Star):
                # The child should already supply every column; this only
                # works in conjunction with a Scan that materialises them.
                # We emit a sentinel "_all" and the executor will replace it.
                self._expanded_columns.append((item.expr, "*"))
            else:
                name = item.alias or _expr_label(item.expr)
                self._expanded_columns.append((item.expr, name))

    def close(self) -> None:
        self.child.close()

    def next(self) -> Row | None:
        row = self.child.next()
        if row is None:
            return None
        out: dict[str, Value] = {}
        for expr, name in self._expanded_columns or []:
            if name == "*":
                # Copy every column from the upstream row.
                for k, v in row.values.items():
                    out[k] = v
            else:
                out[name] = eval_expr(expr, row)
        return Row(out)


class Sort(Operator):
    """Sort all input rows by an ORDER BY list (materialises input)."""

    def __init__(self, child: Operator, keys: tuple[OrderItem, ...]) -> None:
        self.child = child
        self.keys = keys
        self._buffer: list[Row] | None = None
        self._i: int = 0

    def open(self) -> None:
        self.child.open()
        rows: list[Row] = []
        while True:
            r = self.child.next()
            if r is None:
                break
            rows.append(r)
        # Use functools.cmp_to_key so we can mix ASC and DESC across levels.
        from functools import cmp_to_key

        def compare(a: Row, b: Row) -> int:
            for item in self.keys:
                va = eval_expr(item.expr, a)
                vb = eval_expr(item.expr, b)
                c = _compare_values(va, vb)
                if c != 0:
                    return -c if item.desc else c
            return 0

        rows.sort(key=cmp_to_key(compare))
        self._buffer = rows
        self._i = 0

    def close(self) -> None:
        self.child.close()
        self._buffer = None
        self._i = 0

    def next(self) -> Row | None:
        if self._buffer is None or self._i >= len(self._buffer):
            return None
        r = self._buffer[self._i]
        self._i += 1
        return r


def _compare_values(a: Value, b: Value) -> int:
    """SQL 3-valued comparator returning -1 / 0 / 1.

    NULL sorts as the largest value in ASC order (NULLs last). Mixed
    types fall back to tag ordering to remain deterministic.
    """
    if a.is_null and b.is_null:
        return 0
    if a.is_null:
        return 1  # a > b
    if b.is_null:
        return -1  # a < b
    if a.tag in (Tag.INT, Tag.FLOAT) and b.tag in (Tag.INT, Tag.FLOAT):
        x, y = float(a.payload), float(b.payload)
        if x < y:
            return -1
        if x > y:
            return 1
        return 0
    if a.tag is b.tag:
        if a.payload < b.payload:
            return -1
        if a.payload > b.payload:
            return 1
        return 0
    # Incomparable types: fall back to tag ordering.
    if a.tag.value < b.tag.value:
        return -1
    if a.tag.value > b.tag.value:
        return 1
    return 0


class Limit(Operator):
    def __init__(self, child: Operator, n: int) -> None:
        self.child = child
        self.n = n
        self._emitted = 0

    def open(self) -> None:
        self.child.open()
        self._emitted = 0

    def close(self) -> None:
        self.child.close()

    def next(self) -> Row | None:
        if self._emitted >= self.n:
            return None
        r = self.child.next()
        if r is None:
            return None
        self._emitted += 1
        return r


class Offset(Operator):
    def __init__(self, child: Operator, n: int) -> None:
        self.child = child
        self.n = n
        self._skipped = 0

    def open(self) -> None:
        self.child.open()
        self._skipped = 0
        for _ in range(self.n):
            if self.child.next() is None:
                break
            self._skipped += 1

    def close(self) -> None:
        self.child.close()

    def next(self) -> Row | None:
        return self.child.next()


class Distinct(Operator):
    """Eliminate duplicate rows (by full content)."""

    def __init__(self, child: Operator) -> None:
        self.child = child
        self._buffer: list[Row] | None = None
        self._i: int = 0

    def open(self) -> None:
        self.child.open()
        seen: set[tuple[tuple[str, object], ...]] = set()
        out: list[Row] = []
        while True:
            r = self.child.next()
            if r is None:
                break
            # Hash by (col_name, value_payload_or_null) tuples.
            key = tuple((k, v.payload if not v.is_null else None) for k, v in r.values.items())
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        self._buffer = out
        self._i = 0

    def close(self) -> None:
        self.child.close()
        self._buffer = None
        self._i = 0

    def next(self) -> Row | None:
        if self._buffer is None or self._i >= len(self._buffer):
            return None
        r = self._buffer[self._i]
        self._i += 1
        return r


def _expr_label(expr: Expr) -> str:
    """Generate a column-label string for a projected expression."""
    if hasattr(expr, "name") and isinstance(getattr(expr, "name", None), str):
        return getattr(expr, "name")
    return type(expr).__name__