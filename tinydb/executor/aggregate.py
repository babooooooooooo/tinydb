"""Aggregate operator: COUNT, SUM, AVG with optional GROUP BY."""

from __future__ import annotations

from tinydb.executor.expressions import eval_expr
from tinydb.executor.operator import Operator
from tinydb.executor.row import Row
from tinydb.parser.ast import (
    ColumnRef,
    Expr,
    FunctionCall,
    SelectItem,
    Star,
)
from tinydb.types import Tag, Value


class Aggregate(Operator):
    """Reduce rows to a single result row (or one per group)."""

    def __init__(
        self,
        child: Operator,
        items: tuple[SelectItem, ...],
        group_by: tuple[str, ...] = (),
    ) -> None:
        self.child = child
        self.items = items
        self.group_by = group_by
        self._groups: list[Row] | None = None
        self._i: int = 0

    def open(self) -> None:
        self.child.open()
        # Materialise input.
        rows: list[Row] = []
        while True:
            r = self.child.next()
            if r is None:
                break
            rows.append(r)
        # Partition into groups keyed by group_by columns.
        groups: dict[tuple, list[Row]] = {}
        order: list[tuple] = []
        for r in rows:
            key = tuple(r[g] for g in self.group_by)
            if key not in groups:
                order.append(key)
                groups[key] = []
            groups[key].append(r)
        if not groups:
            # SQL: GROUP BY over empty input → 0 rows. The "synthesize
            # an empty group" rule applies only to scalar aggregates
            # (no GROUP BY), where SELECT COUNT(*) FROM empty_t still
            # returns ONE row.
            if self.group_by:
                self._groups = []
                self._i = 0
                return
            order = [()]
            groups = {(): []}
        # Reduce each group.
        out: list[Row] = []
        for gkey in order:
            grows = groups[gkey]
            values: dict[str, Value] = {}
            for col, val in zip(self.group_by, gkey):
                values[col] = val
            for item in self.items:
                values[_item_name(item)] = _reduce(item, grows)
            out.append(Row(values))
        self._groups = out
        self._i = 0

    def close(self) -> None:
        self.child.close()
        self._groups = None

    def next(self) -> Row | None:
        if self._groups is None or self._i >= len(self._groups):
            return None
        r = self._groups[self._i]
        self._i += 1
        return r


def _item_name(item: SelectItem) -> str:
    if item.alias:
        return item.alias
    if isinstance(item.expr, FunctionCall):
        if isinstance(item.expr.arg, Star):
            return f"COUNT(*)"
        return f"{item.expr.name}({_expr_label(item.expr.arg)})"
    return _expr_label(item.expr)


def _expr_label(expr: Expr) -> str:
    if isinstance(expr, ColumnRef):
        return expr.name
    return type(expr).__name__


def _reduce(item: SelectItem, rows: list[Row]) -> Value:
    """Compute a single aggregate value over ``rows`` for one SelectItem."""
    expr = item.expr
    if isinstance(expr, FunctionCall):
        if expr.name == "COUNT":
            if isinstance(expr.arg, Star):
                return Value.int_(len(rows))
            # COUNT(col) ignores NULL.
            n = 0
            for r in rows:
                v = eval_expr(expr.arg, r)
                if not v.is_null:
                    n += 1
            return Value.int_(n)
        if expr.name == "SUM":
            # SQL: SUM over no non-NULL inputs is NULL, not 0. 0 is a
            # legitimate value when at least one operand contributed.
            # Accumulate INTs in the integer domain to avoid float
            # precision loss above 2^53; only promote to float when a
            # FLOAT operand actually contributes.
            int_total = 0
            float_total = 0.0
            saw_float = False
            any_contrib = False
            for r in rows:
                v = eval_expr(expr.arg, r)
                if v.is_null:
                    continue
                if v.tag is Tag.INT:
                    int_total += v.payload
                    any_contrib = True
                elif v.tag is Tag.FLOAT:
                    float_total += v.payload
                    saw_float = True
                    any_contrib = True
            if not any_contrib:
                return Value.null()
            if saw_float:
                return Value.float_(int_total + float_total)
            return Value.int_(int_total)
        if expr.name == "AVG":
            total = 0.0
            n = 0
            for r in rows:
                v = eval_expr(expr.arg, r)
                if not v.is_null and v.tag in (Tag.INT, Tag.FLOAT):
                    total += float(v.payload)
                    n += 1
            if n == 0:
                return Value.null()
            return Value.float_(total / n)
    # Non-aggregate: must be a group-by column or constant.
    if rows:
        return eval_expr(expr, rows[0])
    return Value.null()