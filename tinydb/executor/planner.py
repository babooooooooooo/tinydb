"""Planner: walk the parser AST and build an operator tree.

The planner is responsible for:

* Resolving table names against the catalog.
* Building the scan (SeqScan, or IndexScan when an index matches the WHERE).
* Wrapping the scan in Filter, Sort, Limit, Offset, Aggregate, Project.
* Constructing write operators (CreateTable, DropTable, Insert, Update, Delete).

Every operator is constructed with the references it needs (catalog, pool,
freelist); the planner is a pure function of ``(stmt, catalog, pool,
freelist)`` that returns the root ``Operator``. Lifecycle (open/next/close)
is the caller's responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass

from tinydb.catalog.catalog import Catalog
from tinydb.catalog.schema import TableMeta
from tinydb.executor.aggregate import Aggregate
from tinydb.executor.filter import Distinct, Filter, Limit, Offset, Project, Sort
from tinydb.executor.operator import Operator
from tinydb.executor.scan import IndexScan, SeqScan
from tinydb.executor.write import (
    CreateTable,
    Delete,
    DropTable,
    Insert,
    Update,
)
from tinydb.index.btree import BPlusTree, _key_eq, _key_gt, _key_lt
from tinydb.parser.ast import (
    BeginStmt,
    BinaryOp,
    ColumnRef,
    CommitStmt,
    CreateTableStmt,
    DeleteStmt,
    DropTableStmt,
    InsertStmt,
    Literal,
    RollbackStmt,
    SelectStmt,
    Stmt,
    UnaryOp,
    UpdateStmt,
)
from tinydb.storage.buffer import BufferPool
from tinydb.storage.freelist import FreeList
from tinydb.types.check import coerce
from tinydb.types.value import Tag, Value


@dataclass(frozen=True)
class IndexBound:
    """Inclusive/exclusive scan window over an indexed column.

    ``low`` / ``high`` are the open ends; ``low_inclusive`` /
    ``high_inclusive`` flip each end between ``[`` and ``(``. A ``None``
    bound is open-ended regardless of its inclusive flag.

    ``always_empty=True`` is set when the constraints make it impossible
    for any row to match (e.g. ``col > 10 AND col < 5``); IndexScan handles
    the short-circuit internally.
    """

    column: str
    low: Value | None
    high: Value | None
    low_inclusive: bool = True
    high_inclusive: bool = True
    always_empty: bool = False


def plan(
    stmt: Stmt,
    catalog: Catalog,
    pool: BufferPool,
    freelist: FreeList,
    txn=None,
) -> Operator:
    """Return the root operator for ``stmt``.

    Dispatch on the concrete Stmt subclass.
    """
    if isinstance(stmt, CreateTableStmt):
        return CreateTable(catalog, stmt)
    if isinstance(stmt, DropTableStmt):
        return DropTable(catalog, stmt)
    if isinstance(stmt, InsertStmt):
        return Insert(catalog, pool, freelist, stmt, txn=txn)
    if isinstance(stmt, UpdateStmt):
        return Update(catalog, pool, freelist, stmt, txn=txn)
    if isinstance(stmt, DeleteStmt):
        return Delete(catalog, pool, freelist, stmt, txn=txn)
    if isinstance(stmt, SelectStmt):
        return _plan_select(stmt, catalog, pool, freelist, txn)
    if isinstance(stmt, (BeginStmt, CommitStmt, RollbackStmt)):
        raise NotImplementedError("transaction control not yet implemented in planner")
    raise TypeError(f"unsupported statement: {type(stmt).__name__}")


def _plan_select(
    stmt: SelectStmt,
    catalog: Catalog,
    pool: BufferPool,
    freelist: FreeList,
    txn=None,
) -> Operator:
    if stmt.from_table is None:
        raise ValueError("SELECT must have a FROM clause")
    table = catalog.get_table(stmt.from_table)
    op: Operator = _build_scan(stmt, table, catalog, pool, freelist, txn)
    if stmt.where is not None:
        op = Filter(op, stmt.where)
    if stmt.group_by or _has_aggregate(stmt.items):
        # Aggregate handles both grouped and ungrouped reductions.
        op = Aggregate(op, stmt.items, stmt.group_by)
    # ORDER BY is applied BEFORE projection so that columns referenced
    # only in ORDER BY are still visible to the sort.
    if stmt.order_by:
        op = Sort(op, stmt.order_by)
    if stmt.distinct:
        op = Distinct(op)
    if stmt.offset is not None:
        op = Offset(op, stmt.offset)
    if stmt.limit is not None:
        op = Limit(op, stmt.limit)
    # Projection is the last step (except for aggregates, which project
    # internally as part of their reduction).
    if not stmt.group_by and not _has_aggregate(stmt.items):
        op = Project(op, stmt.items)
    return op


def _has_aggregate(items) -> bool:
    """Return True if any select item is an aggregate function call."""
    from tinydb.parser.ast import FunctionCall

    return any(isinstance(it.expr, FunctionCall) for it in items)


def _build_scan(
    stmt: SelectStmt,
    table: TableMeta,
    catalog: Catalog,
    pool: BufferPool,
    freelist: FreeList,
    txn=None,
) -> Operator:
    """Pick SeqScan or IndexScan.

    When the WHERE can be reduced to a safe [low, high] bound over a
    single indexed column, return an IndexScan carrying that bound;
    otherwise fall back to a SeqScan. ``always_empty`` bounds are still
    served as IndexScan so the operator can short-circuit internally.
    """
    bound = (
        _extract_index_bound(stmt.where, table) if stmt.where is not None else None
    )
    if bound is not None:
        idx = table.index_for(bound.column)
        if idx is not None:
            tree = BPlusTree(pool, freelist, root_page_id=idx.root_page, txn=txn)
            return IndexScan(pool, tree, table, idx.column, bound=bound)
    return SeqScan(pool, table)


# ---- IndexBound extraction -------------------------------------------------
#
# The planner only knows how to push down "safe" predicates: a top-level
# AND chain whose leaves are simple ``col <op> literal`` comparisons. Any
# OR, NOT, <>, arithmetic, or non-trivial nesting causes extraction to
# abort and we fall back to SeqScan + Filter. We further bail when a
# literal fails to coerce to the target column's declared type, since
# that mismatch is the same error the executor would raise at runtime.


def _literal_to_value(lit: Literal) -> Value:
    """Convert an AST ``Literal`` to a ``Value``.

    Booleans must be checked before ints because ``bool`` is a subclass
    of ``int`` in Python.
    """
    if lit.value is None:
        return Value.null()
    if isinstance(lit.value, bool):
        return Value.bool_(lit.value)
    if isinstance(lit.value, int):
        return Value.int_(lit.value)
    if isinstance(lit.value, float):
        return Value.float_(lit.value)
    if isinstance(lit.value, str):
        return Value.text(lit.value)
    raise TypeError(f"unsupported literal value: {lit.value!r}")


def _flip_op(op: str) -> str | None:
    """Flip ``op`` so the column is on the left side, or None if not flippable.

    Symmetric ops (``=``, ``<>``) stay unchanged; range ops swap.
    """
    if op in ("=", "<>"):
        return op
    return {">": "<", "<": ">", ">=": "<=", "<=": ">="}.get(op)


def _normalize_comparison(expr: BinaryOp) -> tuple[str | None, str | None, Value | None]:
    """Return ``(column, op, value)`` with column on the left, or all None.

    A ``None`` (NULL) literal on either side is treated as unsafe — SQL
    comparisons with NULL are UNKNOWN and there's no bound to extract.
    """
    left, right = expr.left, expr.right
    op = expr.op
    if isinstance(left, ColumnRef) and isinstance(right, Literal):
        if right.value is None:
            return None, None, None
        return left.name, op, _literal_to_value(right)
    if isinstance(right, ColumnRef) and isinstance(left, Literal):
        if left.value is None:
            return None, None, None
        flipped = _flip_op(op)
        if flipped is None:
            return None, None, None
        return right.name, flipped, _literal_to_value(left)
    return None, None, None


def _collect_comparisons(expr) -> list[tuple[str, str, Value]] | None:
    """Recursively collect safe comparisons from a WHERE expression.

    Walks a top-level AND tree, gathering ``(column, op, value)`` tuples
    from each ``col <op> literal`` leaf. Returns ``None`` when the tree
    contains an unsafe construct (OR, NOT, <>, arithmetic) — callers must
    treat ``None`` as "fall back to SeqScan".
    """
    if isinstance(expr, BinaryOp):
        if expr.op == "AND":
            left = _collect_comparisons(expr.left)
            if left is None:
                return None
            right = _collect_comparisons(expr.right)
            if right is None:
                return None
            return left + right
        if expr.op == "<>":
            return None  # not-equal can't be pushed into a [low, high] bound
        if expr.op in ("=", "<", ">", "<=", ">="):
            col, op, value = _normalize_comparison(expr)
            if col is None:
                return None
            return [(col, op, value)]
        # Arithmetic (+, -, *, /) or OR — unsafe.
        return None
    if isinstance(expr, UnaryOp):
        # NOT inverts truth; we can't safely pull a bound out of it.
        return None
    # Bare Literal / ColumnRef / Star — nothing to extract.
    return []


def _finalize_bound(
    column: str, predicates: list[tuple[str, Value]]
) -> IndexBound:
    """Fold a single column's predicates into an ``IndexBound``.

    Tightens the low/high ends across the list (so multiple ``col > 3`` /
    ``col > 5`` collapse to ``col > 5``) and flags ``always_empty`` when
    the constraints conflict — most commonly ``low > high`` or
    ``low == high`` with at least one exclusive end.
    """
    low_value: Value | None = None
    low_inclusive = True
    high_value: Value | None = None
    high_inclusive = True
    always_empty = False

    for op, value in predicates:
        if op == "=":
            if low_value is not None and not _key_eq(low_value, value):
                always_empty = True
            if high_value is not None and not _key_eq(high_value, value):
                always_empty = True
            low_value = value
            low_inclusive = True
            high_value = value
            high_inclusive = True
        elif op == "<":
            if high_value is None or _key_lt(value, high_value):
                high_value = value
                high_inclusive = False
            elif _key_eq(value, high_value) and high_inclusive:
                high_value = value
                high_inclusive = False
        elif op == "<=":
            if high_value is None or _key_lt(value, high_value):
                high_value = value
                high_inclusive = True
            elif _key_eq(value, high_value) and not high_inclusive:
                high_value = value
                high_inclusive = True
        elif op == ">":
            if low_value is None or _key_gt(value, low_value):
                low_value = value
                low_inclusive = False
            elif _key_eq(value, low_value) and low_inclusive:
                low_value = value
                low_inclusive = False
        elif op == ">=":
            if low_value is None or _key_gt(value, low_value):
                low_value = value
                low_inclusive = True
            elif _key_eq(value, low_value) and not low_inclusive:
                low_value = value
                low_inclusive = True

    if low_value is not None and high_value is not None:
        if _key_gt(low_value, high_value):
            always_empty = True
        elif _key_eq(low_value, high_value) and not (low_inclusive and high_inclusive):
            always_empty = True

    return IndexBound(
        column=column,
        low=low_value,
        high=high_value,
        low_inclusive=low_inclusive,
        high_inclusive=high_inclusive,
        always_empty=always_empty,
    )


def _extract_index_bound(expr, table: TableMeta) -> IndexBound | None:
    """Extract an ``IndexBound`` from a WHERE expression, or None.

    Returns ``None`` when:
      * no predicate targets an indexed column,
      * the expression contains OR / NOT / <> / arithmetic,
      * any literal fails to coerce to its target column's declared type,
      * any indexed column has a NULL literal operand.
    """
    comparisons = _collect_comparisons(expr)
    if not comparisons:
        return None

    # Group by column. Predicates on non-indexed columns are dropped.
    by_column: dict[str, list[tuple[str, Value]]] = {}
    for col, op, value in comparisons:
        col_meta = table.column(col)
        if col_meta is None:
            continue
        if table.index_for(col) is None:
            continue
        try:
            coerced = coerce(value, col_meta.type, col_meta.params)
        except Exception:
            return None  # any coerce failure aborts extraction
        by_column.setdefault(col, []).append((op, coerced))

    if not by_column:
        return None

    # Pick the first indexed column (insertion order is deterministic).
    column = next(iter(by_column))
    return _finalize_bound(column, by_column[column])
