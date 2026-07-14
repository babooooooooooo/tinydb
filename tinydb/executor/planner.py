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
from tinydb.index.btree import BPlusTree
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
    UpdateStmt,
)
from tinydb.storage.buffer import BufferPool
from tinydb.storage.freelist import FreeList


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

    An index is used when the WHERE contains a single equality comparison
    ``col = literal`` on a column that has an index. Any conjunction that
    includes such a predicate still qualifies; other shapes fall back to
    SeqScan.
    """
    eq_col = _equality_column(stmt.where, table) if stmt.where is not None else None
    if eq_col is not None:
        idx = table.index_for(eq_col)
        if idx is not None:
            tree = BPlusTree(pool, freelist, root_page_id=idx.root_page, txn=txn)
            return IndexScan(pool, tree, table, idx.column)
    return SeqScan(pool, table)


def _equality_column(where, table: TableMeta) -> str | None:
    """Return the column name of a simple equality predicate, or None.

    Handles ``col = literal`` directly, and any conjunctive (``AND``)
    chain containing such a predicate. Other shapes return None and the
    caller falls back to SeqScan.
    """
    def _visit(expr) -> str | None:
        if isinstance(expr, BinaryOp) and expr.op == "=":
            if isinstance(expr.left, ColumnRef) and isinstance(expr.right, Literal):
                col = expr.left.name
                if table.column(col) is not None:
                    return col
            if isinstance(expr.right, ColumnRef) and isinstance(expr.left, Literal):
                col = expr.right.name
                if table.column(col) is not None:
                    return col
        if isinstance(expr, BinaryOp) and expr.op == "AND":
            a = _visit(expr.left)
            b = _visit(expr.right)
            return a if a is not None else b
        return None

    return _visit(where)
