"""Write operators: CreateTable, DropTable, Insert, Update, Delete.

Each write operator interacts with the catalog, the buffer pool, the heap
pages, and (when indexes are involved) the B+ tree.
"""

from __future__ import annotations

import struct

from tinydb.catalog.catalog import Catalog
from tinydb.catalog.schema import (
    ColumnMeta,
    Constraint,
    IndexMeta,
    TableMeta,
)
from tinydb.errors import ConstraintError, StorageError
from tinydb.executor.expressions import eval_expr, is_truthy
from tinydb.executor.heap import (
    decode_row,
    encode_row,
    mark_deleted,
    row_fits,
)
from tinydb.executor.operator import Operator
from tinydb.executor.row import Row
from tinydb.executor.scan import IndexScan
from tinydb.index.btree import BPlusTree
from tinydb.parser.ast import Assignment, CreateTableStmt, DeleteStmt, InsertStmt
from tinydb.storage.buffer import BufferPool
from tinydb.storage.freelist import FreeList
from tinydb.storage.page import PAGE_HEADER_SIZE, PAGE_SIZE, Page, PageType
from tinydb.types import Tag, Value
from tinydb.types.check import coerce


# ============================================================================
# Catalog mutators
# ============================================================================


class CreateTable(Operator):
    """Add a new table to the catalog; no rows are produced."""

    def __init__(self, catalog: Catalog, stmt: CreateTableStmt) -> None:
        self.catalog = catalog
        self.stmt = stmt

    def open(self) -> None:
        cols: list[ColumnMeta] = []
        for c in self.stmt.columns:
            mask = 0
            if c.not_null:
                mask |= Constraint.NOT_NULL
            if c.primary_key:
                mask |= Constraint.PRIMARY_KEY
            if c.unique:
                mask |= Constraint.UNIQUE
            cols.append(
                ColumnMeta(
                    name=c.name, type=c.type, constraints=mask, params=c.params
                )
            )
        self.catalog.create_table(self.stmt.name, cols)
        # PRIMARY KEY and UNIQUE columns get an auto-index so Insert can
        # enforce uniqueness without scanning the heap. The index root
        # page is allocated lazily on the first insert.
        for c in cols:
            if c.is_primary_key or c.is_unique:
                idx = IndexMeta(
                    name=f"_auto_{c.name}",
                    column=c.name,
                    is_unique=True,
                    root_page=0,
                )
                self.catalog.add_index(self.stmt.name, idx)

    def next(self) -> Row | None:
        return None

    def close(self) -> None:
        pass


class DropTable(Operator):
    def __init__(self, catalog: Catalog, stmt) -> None:
        self.catalog = catalog
        self.stmt = stmt

    def open(self) -> None:
        self.catalog.drop_table(self.stmt.name)
        self.catalog.save()

    def next(self) -> Row | None:
        return None

    def close(self) -> None:
        pass


# ============================================================================
# Insert
# ============================================================================


class Insert(Operator):
    """Insert one row per VALUES tuple; enforce type, NULL, unique constraints.

    Index maintenance: for every UNIQUE / PRIMARY KEY index on the table,
    check that the new key does not already exist in the B+ tree.
    """

    def __init__(
        self,
        catalog: Catalog,
        pool: BufferPool,
        freelist: FreeList,
        stmt: InsertStmt,
        txn=None,
    ) -> None:
        self.catalog = catalog
        self.pool = pool
        self.freelist = freelist
        self.txn = txn
        self.stmt = stmt
        self.rows_affected = 0
        self._emitted = False

    def open(self) -> None:
        meta = self.catalog.get_table(self.stmt.table)
        cols = list(meta.columns)
        if self.stmt.columns is None:
            target_cols = cols
            values = list(self.stmt.values)
        else:
            name_to_col = {c.name: c for c in cols}
            target_cols = [name_to_col[n] for n in self.stmt.columns]
            values = list(self.stmt.values)
        if len(values) != len(target_cols):
            raise ConstraintError(
                f"INSERT expects {len(target_cols)} values, got {len(values)}"
            )
        # Evaluate each value.
        raw: dict[str, Value] = {}
        for col, expr in zip(target_cols, values):
            v = _eval_expr(expr, Row({}))
            if v.is_null and (col.is_not_null or col.is_primary_key):
                raise ConstraintError(
                    f"column {col.name!r}: NULL violates NOT NULL"
                )
            v = coerce(v, col.type, col.params)
            raw[col.name] = v
        # Build full row in declared-column order. Columns not mentioned
        # in the INSERT statement default to NULL (subject to NOT NULL).
        full_values: list[Value] = []
        for c in cols:
            if c.name in raw:
                full_values.append(raw[c.name])
            else:
                if c.is_not_null or c.is_primary_key:
                    raise ConstraintError(
                        f"column {c.name!r}: NULL violates NOT NULL"
                    )
                full_values.append(Value.null())
        # Uniqueness must be checked against the live indexes BEFORE we
        # mutate any page.
        self._check_unique_keys(meta, raw)
        # Append to the heap; returns (page_id, row_offset) for the new row.
        row_pid, row_off = self._append_row(meta, full_values)
        # Re-read the meta to pick up any heap-page allocation.
        meta = self.catalog.get_table(self.stmt.table)
        # Insert the new (key, row_id) into every applicable index.
        self._update_indexes_on_insert(meta, raw, row_pid, row_off)
        # Re-read AGAIN to capture the index root_page updates and bump row count.
        meta = self.catalog.get_table(self.stmt.table)
        new_meta = TableMeta(
            name=meta.name,
            columns=meta.columns,
            heap_first_page=meta.heap_first_page,
            heap_last_page=meta.heap_last_page,
            indexes=meta.indexes,
            row_count=meta.row_count + 1,
        )
        self.catalog.update_table(new_meta)
        self.rows_affected = 1

    def close(self) -> None:
        pass

    def next(self) -> Row | None:
        # Insert is not a row stream.
        return None

    # ---- internals -------------------------------------------------------

    def _check_unique_keys(self, meta: TableMeta, row: dict[str, Value]) -> None:
        pk = meta.primary_key()
        if pk is not None and pk.name in row:
            key = row[pk.name]
            tree = _open_index_tree(self.catalog, self.pool, self.freelist, meta, pk.name, txn=self.txn)
            try:
                if tree.point_lookup(key) is not None:
                    raise ConstraintError(
                        f"duplicate primary key {key!r} on column {pk.name!r}"
                    )
            finally:
                pass  # tree may be unbound; nothing to close here

        for idx in meta.indexes:
            if not idx.is_unique:
                continue
            if idx.column not in row:
                continue
            key = row[idx.column]
            tree = _open_index_tree(self.catalog, self.pool, self.freelist, meta, idx.column, txn=self.txn)
            if tree.point_lookup(key) is not None:
                raise ConstraintError(
                    f"duplicate unique key {key!r} on column {idx.column!r}"
                )

    def _append_row(self, meta: TableMeta, values: list[Value]) -> int:
        """Append the row and return its (page_id, offset) within the heap."""
        encoded = encode_row(values)
        if meta.heap_last_page == 0:
            # First page for this table.
            page = self.freelist.allocate(PageType.HEAP)
            page._write_header()
            # Capture pre-image for rollback (none here since the page is fresh).
            self._log_page_if_txn(page)
            self.pool.register_page(page)
            meta_table = _with_heap(meta, first=page.page_id, last=page.page_id)
            self.catalog.update_table(meta_table)
            offset = self._append_to_page(page, encoded)
            return page.page_id, offset
        page = self.pool.fetch_page(meta.heap_last_page)
        try:
            # Capture pre-image BEFORE any mutation.
            self._log_page_if_txn(page)
            if PAGE_HEADER_SIZE + len(encoded) + (page.free_offset - PAGE_HEADER_SIZE) <= PAGE_SIZE \
                    and row_fits(values, page):
                offset = self._append_to_page(page, encoded)
                return page.page_id, offset
            else:
                new_page = self.freelist.allocate(PageType.HEAP)
                new_page._write_header()
                self._log_page_if_txn(new_page)
                self.pool.register_page(new_page)
                old_next = page.next
                new_page.next = old_next
                page.next = new_page.page_id
                if old_next != 0:
                    nxt = self.pool.fetch_page(old_next)
                    nxt.prev = new_page.page_id
                    self.pool.unpin_page(old_next, dirty=True)
                new_page.prev = meta.heap_last_page
                meta_table = _with_heap(meta, last=new_page.page_id)
                self.catalog.update_table(meta_table)
                offset = self._append_to_page(new_page, encoded)
                self.pool.unpin_page(page.page_id, dirty=True)
                return new_page.page_id, offset
        finally:
            self.pool.unpin_page(page.page_id, dirty=page.dirty)

    def _log_page_if_txn(self, page: Page) -> None:
        """Forward the page's pre-image to the txn manager if one is open."""
        log_page_if_txn(self.txn, page)

    def _append_to_page(self, page: Page, encoded: bytes) -> int:
        offset = page.free_offset
        page.data[offset : offset + len(encoded)] = encoded
        page.free_offset = offset + len(encoded)
        page.num_slots += 1
        page.dirty = True
        return offset

    def _update_indexes_on_insert(
        self, meta: TableMeta, row: dict[str, Value], row_pid: int, row_off: int
    ) -> None:
        for idx in meta.indexes:
            if idx.column not in row:
                continue
            key = row[idx.column]
            tree = _open_index_tree(self.catalog, self.pool, self.freelist, meta, idx.column, txn=self.txn)
            row_id = IndexScan.encode_row_id(row_pid, row_off)
            tree.insert(key, row_id)
            # Persist any root-page change (initial allocation OR a new
            # internal root grown out of leaf splits). Without this, a
            # tree whose root has been promoted since the catalog was
            # last refreshed would lose ``idx.root_page`` across a close
            # and the next reader would descend into a stale leaf.
            if tree.root_page_id != idx.root_page and tree.root_page_id != 0:
                new_idx = IndexMeta(
                    name=idx.name,
                    column=idx.column,
                    is_unique=idx.is_unique,
                    root_page=tree.root_page_id,
                )
                meta = self.catalog.get_table(meta.name)
                updated = tuple(
                    new_idx if i.name == idx.name else i for i in meta.indexes
                )
                self.catalog.update_table(
                    TableMeta(
                        name=meta.name,
                        columns=meta.columns,
                        heap_first_page=meta.heap_first_page,
                        heap_last_page=meta.heap_last_page,
                        indexes=updated,
                        row_count=meta.row_count,
                    )
                )

    def _last_row_offset(self, meta: TableMeta, page_id: int) -> int:
        page = self.pool.fetch_page(page_id)
        try:
            return page.free_offset - _row_size_at(page, page.free_offset)
        finally:
            self.pool.unpin_page(page_id, dirty=False)


# ============================================================================
# Update
# ============================================================================


class Update(Operator):
    """Update rows matching a WHERE; rebuild affected indexes on the fly."""

    def __init__(
        self,
        catalog: Catalog,
        pool: BufferPool,
        freelist: FreeList,
        stmt,  # UpdateStmt
        txn=None,
    ) -> None:
        self.catalog = catalog
        self.pool = pool
        self.freelist = freelist
        self.txn = txn
        self.stmt = stmt
        self.rows_affected = 0
        self._done = False

    def open(self) -> None:
        meta = self.catalog.get_table(self.stmt.table)
        col_lookup = {c.name: c for c in meta.columns}
        # Snapshot matching rows.
        matches = _scan_table(self.pool, meta)
        survivors: list[tuple[int, int, dict[str, Value], dict[str, Value]]] = []
        # (page, off, old_values, new_values)
        for page_id, offset, values in matches:
            row = Row(dict(zip([c.name for c in meta.columns], values)))
            if self.stmt.where is not None:
                v = eval_expr(self.stmt.where, row)
                t = is_truthy(v)
                if t is not True:
                    continue
            # Build new value dict.
            old = {c.name: v for c, v in zip(meta.columns, values)}
            new_vals = dict(old)
            for asn in self.stmt.assignments:
                if asn.column not in col_lookup:
                    raise StorageError(f"no such column: {asn.column}")
                col = col_lookup[asn.column]
                v = _eval_expr(asn.value, row)
                if v.is_null and (col.is_not_null or col.is_primary_key):
                    raise ConstraintError(
                        f"column {col.name!r}: NULL violates NOT NULL"
                    )
                v = coerce(v, col.type, col.params)
                new_vals[asn.column] = v
            # Check uniqueness for any changed PK / unique columns.
            self._check_unique_after_update(meta, new_vals, old)
            survivors.append((page_id, offset, old, new_vals))
        # Apply updates.
        for page_id, offset, old_vals, new_vals in survivors:
            new_loc = self._rewrite_row(page_id, offset, meta, list(new_vals.values()))
            self._update_indexes_after_update(
                meta, new_vals, old_vals, new_loc
            )
        self.rows_affected = len(survivors)
        self._done = True

    def close(self) -> None:
        pass

    def next(self) -> Row | None:
        return None

    def _rewrite_row(
        self, page_id: int, offset: int, meta: TableMeta, values: list[Value]
    ) -> tuple[int, int]:
        """Rewrite a row in place (or append a new copy), return (page_id, offset)."""
        page = self.pool.fetch_page(page_id)
        try:
            # Capture pre-image BEFORE any mutation, for rollback.
            log_page_if_txn(self.txn, page)
            # Read existing row length.
            (raw_len,) = struct.unpack_from("<H", page.data, offset)
            old_len = raw_len & 0x7FFF
            new_encoded = encode_row(values)
            if len(new_encoded) <= old_len:
                # Overwrite in place; pad with zeros.
                page.data[offset : offset + len(new_encoded)] = new_encoded
                # Zero out the slack so old bytes don't confuse a reader.
                for i in range(len(new_encoded), old_len):
                    page.data[offset + i] = 0
                page.dirty = True
                return page_id, offset
            # New row is larger; mark old as deleted and append at end.
            mark_deleted(page.data, offset)
            new_offset = page.free_offset
            page.data[new_offset : new_offset + len(new_encoded)] = new_encoded
            page.free_offset = new_offset + len(new_encoded)
            page.num_slots += 1
            page.dirty = True
            return page_id, new_offset
        finally:
            self.pool.unpin_page(page_id, dirty=page.dirty)

    def _check_unique_after_update(
        self, meta: TableMeta, new_vals: dict[str, Value], old: dict[str, Value]
    ) -> None:
        pk = meta.primary_key()
        if pk is not None and pk.name in new_vals and new_vals[pk.name] != old.get(pk.name):
            tree = _open_index_tree(self.catalog, self.pool, self.freelist, meta, pk.name, txn=self.txn)
            if tree.point_lookup(new_vals[pk.name]) is not None:
                raise ConstraintError(
                    f"duplicate primary key {new_vals[pk.name]!r} on column {pk.name!r}"
                )
        for idx in meta.indexes:
            if not idx.is_unique:
                continue
            if idx.column not in new_vals:
                continue
            if new_vals[idx.column] == old.get(idx.column):
                continue
            tree = _open_index_tree(self.catalog, self.pool, self.freelist, meta, idx.column, txn=self.txn)
            if tree.point_lookup(new_vals[idx.column]) is not None:
                raise ConstraintError(
                    f"duplicate unique key {new_vals[idx.column]!r} on column {idx.column!r}"
                )

    def _update_indexes_after_update(
        self,
        meta: TableMeta,
        new_row: dict[str, Value],
        old_row: dict[str, Value],
        new_loc: tuple[int, int],
    ) -> None:
        new_pid, new_off = new_loc
        for idx in meta.indexes:
            if idx.column not in new_row:
                continue
            new_key = new_row[idx.column]
            old_key = old_row.get(idx.column)
            tree = _open_index_tree(self.catalog, self.pool, self.freelist, meta, idx.column, txn=self.txn)
            if old_key is not None and not _values_equal(old_key, new_key):
                tree.delete(old_key)
            new_id = IndexScan.encode_row_id(new_pid, new_off)
            tree.insert(new_key, new_id)


# ============================================================================
# Delete
# ============================================================================


class Delete(Operator):
    def __init__(
        self,
        catalog: Catalog,
        pool: BufferPool,
        freelist: FreeList,
        stmt: DeleteStmt,
        txn=None,
    ) -> None:
        self.catalog = catalog
        self.pool = pool
        self.freelist = freelist
        self.txn = txn
        self.stmt = stmt
        self.rows_affected = 0
        self._done = False

    def open(self) -> None:
        meta = self.catalog.get_table(self.stmt.table)
        matches = _scan_table(self.pool, meta)
        to_delete: list[tuple[int, int, dict[str, Value]]] = []
        col_names = [c.name for c in meta.columns]
        for page_id, offset, values in matches:
            row = Row(dict(zip(col_names, values)))
            if self.stmt.where is not None:
                v = eval_expr(self.stmt.where, row)
                t = is_truthy(v)
                if t is not True:
                    continue
            old = {n: v for n, v in zip(col_names, values)}
            to_delete.append((page_id, offset, old))
        for page_id, offset, row in to_delete:
            self._delete_row(page_id, offset, meta, row)
        self.rows_affected = len(to_delete)
        # Update row_count.
        new_meta = TableMeta(
            name=meta.name,
            columns=meta.columns,
            heap_first_page=meta.heap_first_page,
            heap_last_page=meta.heap_last_page,
            indexes=meta.indexes,
            row_count=max(0, meta.row_count - len(to_delete)),
        )
        self.catalog.update_table(new_meta)
        self._done = True

    def close(self) -> None:
        pass

    def next(self) -> Row | None:
        return None

    def _delete_row(
        self, page_id: int, offset: int, meta: TableMeta, row: dict[str, Value]
    ) -> None:
        page = self.pool.fetch_page(page_id)
        try:
            # Capture pre-image BEFORE mutation, for rollback.
            log_page_if_txn(self.txn, page)
            mark_deleted(page.data, offset)
            page.dirty = True
        finally:
            self.pool.unpin_page(page_id, dirty=page.dirty)
        # Remove from indexes.
        for idx in meta.indexes:
            if idx.column not in row:
                continue
            tree = _open_index_tree(self.catalog, self.pool, self.freelist, meta, idx.column, txn=self.txn)
            tree.delete(row[idx.column])


# ============================================================================
# Shared helpers
# ============================================================================


def _eval_expr(expr, row: Row) -> Value:
    """Evaluate an expression in the context of ``row`` (or a literal).

    Used by write operators: a SET value may be a literal (no row context)
    or a column reference / arithmetic expression referencing the current
    row, e.g. ``SET balance = balance - 1``.
    """
    from tinydb.parser.ast import BinaryOp, ColumnRef, Literal, UnaryOp

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
    if isinstance(expr, ColumnRef):
        return row[expr.name]
    if isinstance(expr, UnaryOp):
        if expr.op == "-":
            inner = _eval_expr(expr.operand, row)
            if inner.is_null:
                return Value.null()
            if inner.tag is Tag.INT:
                return Value.int_(-int(inner.payload))
            if inner.tag is Tag.FLOAT:
                return Value.float_(-float(inner.payload))
            raise TypeError(f"unary - on non-numeric: {inner.tag}")
        raise TypeError(f"unsupported unary op: {expr.op}")
    if isinstance(expr, BinaryOp):
        left = _eval_expr(expr.left, row)
        right = _eval_expr(expr.right, row)
        return _eval_arith(expr.op, left, right)
    raise TypeError(f"unsupported expression: {expr!r}")


def _eval_arith(op: str, left: Value, right: Value) -> Value:
    if left.is_null or right.is_null:
        return Value.null()
    if left.tag not in (Tag.INT, Tag.FLOAT) or right.tag not in (Tag.INT, Tag.FLOAT):
        raise TypeMismatchError(
            f"arithmetic on non-numeric: {left.tag} {op} {right.tag}"
        )
    a = float(left.payload)
    b = float(right.payload)
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
    if left.tag is Tag.INT and right.tag is Tag.INT and op != "/":
        return Value.int_(int(result))
    return Value.float_(result)


def _values_equal(a: Value, b: Value) -> bool:
    if a.is_null and b.is_null:
        return True
    if a.is_null or b.is_null:
        return False
    if a.tag in (Tag.INT, Tag.FLOAT) and b.tag in (Tag.INT, Tag.FLOAT):
        return float(a.payload) == float(b.payload)
    return a.tag is b.tag and a.payload == b.payload


def _with_heap(meta: TableMeta, first: int | None = None, last: int | None = None) -> TableMeta:
    return TableMeta(
        name=meta.name,
        columns=meta.columns,
        heap_first_page=meta.heap_first_page if first is None else first,
        heap_last_page=meta.heap_last_page if last is None else last,
        indexes=meta.indexes,
        row_count=meta.row_count,
    )


def log_page_if_txn(txn, page: Page) -> None:
    """Forward the page's pre-image to the txn manager if a txn is open.

    Shared by Insert / Update / Delete so each can capture pre-images
    before mutating heap pages.
    """
    if txn is not None and txn.in_transaction:
        txn.log_page_write(page)


def _open_index_tree(
    catalog: Catalog, pool: BufferPool, freelist: FreeList, meta: TableMeta, column: str,
    txn=None,
) -> BPlusTree:
    idx = meta.index_for(column)
    if idx is None:
        # Return a transient tree on an empty root.
        return BPlusTree(pool, freelist, root_page_id=0, txn=txn)
    return BPlusTree(pool, freelist, root_page_id=idx.root_page, txn=txn)


def _scan_table(
    pool: BufferPool, meta: TableMeta
) -> list[tuple[int, int, list[Value]]]:
    """Return every (page_id, row_offset, values) for LIVE rows in the table.

    Tombstoned rows are skipped; otherwise DELETE WHERE pk=X would
    re-match an already-deleted row on a subsequent call and over-report
    ``rows_affected``.
    """
    out: list[tuple[int, int, list[Value]]] = []
    pid = meta.heap_first_page
    while pid != 0:
        page = pool.fetch_page(pid)
        try:
            data = bytes(page.data)
            offset = PAGE_HEADER_SIZE
            while offset < page.free_offset:
                values, next_off, deleted = decode_row(data, offset)
                if not deleted:
                    out.append((pid, offset, values))
                offset = next_off
            nxt = page.next
        finally:
            pool.unpin_page(page.page_id, dirty=False)
        pid = nxt
    return out


def _row_size_at(page: Page, end_offset: int) -> int:
    """Walk backward from ``end_offset`` to find the most recent row's length.

    Used by Insert to compute the offset of the row just appended so the
    index can reference it.
    """
    # The row at the end was encoded with a 2-byte length prefix; read it.
    if end_offset < PAGE_HEADER_SIZE + 2:
        return 0
    (raw_len,) = struct.unpack_from("<H", page.data, end_offset - 2)
    return raw_len & 0x7FFF