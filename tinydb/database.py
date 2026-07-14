"""Database: top-level entry point for opening a tinydb file and running SQL.

Lifecycle:

    db = Database("path/to/file.db")
    rs = db.execute("CREATE TABLE users (id INT PRIMARY KEY, name TEXT)")
    rs = db.execute("INSERT INTO users VALUES (1, 'alice')")
    rs = db.execute("SELECT * FROM users")
    db.close()

Wiring:

    DiskManager  → owns the file and page-level read/write
    FreeList     → allocator/deallocator of page ids
    BufferPool   → in-memory cache with LRU eviction
    Catalog      → persistent schema (tables, columns, indexes)
    Executor     → Volcano operator tree driving rows to the caller

The Database is a single process, single writer. Concurrency is out of
scope for this MVP.
"""

from __future__ import annotations

import copy
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from tinydb.catalog.catalog import Catalog
from tinydb.errors import TinyDBError, TransactionError
from tinydb.executor.operator import Operator
from tinydb.executor.planner import plan
from tinydb.executor.result import ResultSet
from tinydb.executor.row import Row
from tinydb.parser.parser import parse
from tinydb.storage.buffer import BufferPool
from tinydb.storage.disk import DiskManager
from tinydb.storage.freelist import FreeList
from tinydb.txn.manager import TransactionManager
from tinydb.types import Tag, Value

logger = logging.getLogger(__name__)


class Database:
    """An open tinydb database file.

    Construct with the file path; the file is created if it does not
    exist. Use :meth:`execute` to run SQL; use :meth:`close` to flush
    the catalog and release resources. The Database is not safe to use
    after :meth:`close`.
    """

    def __init__(self, path: str | os.PathLike[str], *, buffer_capacity: int = 64) -> None:
        self.path = Path(path)
        self.disk = DiskManager(self.path)
        self.disk.open()
        # Transaction manager + WAL live next to the data file.
        self.txn = TransactionManager(
            self.disk,
            None,  # pool wired below after construction
            self.path.with_suffix(self.path.suffix + ".wal"),
        )
        # Buffer pool routes dirty writes through the txn manager so
        # every page mutation is logged in the WAL.
        self.pool = BufferPool(
            self.disk,
            capacity=buffer_capacity,
            flush_hook=self.txn.flush_page,
        )
        self.txn.pool = self.pool
        self.freelist = FreeList(self.disk, self.pool, self.txn)
        self.catalog = Catalog(self.disk, self.freelist, self.txn)
        self.catalog.load()
        self.txn.open()
        # If the WAL has records from a previous session, replay them.
        self.txn.recover()
        # Wire up catalog snapshot/restore for transactions.
        self.txn.set_state_callbacks(
            snapshot=self._txn_snapshot,
            restore=self._txn_restore,
        )
        self._closed = False

    def _txn_snapshot(self) -> dict:
        """Deep-copy non-page state (catalog tables + FileHeader)."""
        return {
            "tables": copy.deepcopy(self.catalog._tables),
            "header": self.disk.read_header(),
        }

    def _txn_restore(self, snap: dict) -> None:
        """Restore catalog tables and FileHeader to the snapshot state."""
        self.catalog._tables = copy.deepcopy(snap["tables"])
        # Restore header on disk so file size and freelist match what
        # the catalog reports. This is harmless if nothing in the txn
        # touched the header (we just rewrite the same bytes).
        self.txn.flush_header(snap["header"])

    # ---- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Flush the catalog and close the underlying file.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        if self._closed:
            return
        try:
            if self.txn.in_transaction:
                # Implicit rollback on close.
                try:
                    self.txn.rollback()
                except Exception:
                    logger.warning(
                        "implicit rollback failed during close; database may "
                        "be left in an uncommitted state", exc_info=True
                    )
            # Persist the catalog (updates FileHeader + catalog pages).
            self.catalog.save()
            # Flush any remaining dirty data pages; this also routes
            # through the WAL via the buffer pool's flush_hook.
            self.pool.flush_all()
        finally:
            self.txn.close()
            self.disk.close()
            self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---- public API -------------------------------------------------------

    def execute(self, sql: str) -> ResultSet:
        """Parse, plan, and execute one or more ``;``-separated statements.

        Returns the ``ResultSet`` of the *last* statement. Earlier
        statements are executed for their side effects; their result
        sets are discarded. This matches the conventional REPL behavior
        where each statement is run in order.

        Transaction control statements (BEGIN/COMMIT/ROLLBACK) are
        intercepted and routed to :meth:`begin`, :meth:`commit`,
        :meth:`rollback`.
        """
        self._ensure_open()
        stmts = parse(sql)
        if not stmts:
            return ResultSet()
        last: ResultSet = ResultSet()
        for stmt in stmts:
            last = self._run_one(stmt)
        return last

    def executemany(self, sql: str) -> list[ResultSet]:
        """Execute a multi-statement script and return every result set."""
        self._ensure_open()
        stmts = parse(sql)
        out: list[ResultSet] = []
        for stmt in stmts:
            out.append(self._run_one(stmt))
        return out

    # ---- transaction control ---------------------------------------------

    def begin(self) -> None:
        self._ensure_open()
        self.txn.begin()

    def commit(self) -> None:
        self._ensure_open()
        self.txn.commit()

    def rollback(self) -> None:
        self._ensure_open()
        self.txn.rollback()

    # ---- internals --------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise TinyDBError("database is closed")

    def _run_one(self, stmt) -> ResultSet:
        from tinydb.parser.ast import BeginStmt, CommitStmt, RollbackStmt

        if isinstance(stmt, BeginStmt):
            self.begin()
            return ResultSet()
        if isinstance(stmt, CommitStmt):
            self.commit()
            return ResultSet()
        if isinstance(stmt, RollbackStmt):
            self.rollback()
            return ResultSet()
        op = plan(stmt, self.catalog, self.pool, self.freelist, self.txn)
        rs = self._execute_op(op)
        # Persist the catalog AND flush dirty data pages after every
        # auto-commit write so a crash before db.close() doesn't leave
        # the on-disk catalog stale. Without this, schema/index-root
        # mutations done by INSERT/UPDATE/DELETE only land on disk at
        # close time — a crash in between would cause a later session
        # to start from an out-of-date catalog (e.g. index.root_page=0
        # for a PK index whose leaf is on disk but unreferenced, which
        # silently disables uniqueness enforcement).
        if self._is_write_op(op) and not self.txn.in_transaction:
            self.pool.flush_all()
            self.catalog.save()
        return rs

    def _execute_op(self, op: Operator) -> ResultSet:
        op.open()
        try:
            rs = ResultSet()
            # Read side: build columns and rows from a streaming operator.
            if self._is_write_op(op):
                # Writes have no result rows but expose rows_affected.
                rs.rows_affected = self._consume_write(op)
                return rs
            # Read: project the first row to learn the columns, then drain.
            first = op.next()
            if first is not None:
                rs.columns = list(first.values.keys())
                rs.rows.append(self._stringify_row(first))
                while True:
                    nxt = op.next()
                    if nxt is None:
                        break
                    rs.rows.append(self._stringify_row(nxt))
            return rs
        finally:
            op.close()

    @staticmethod
    def _is_write_op(op: Operator) -> bool:
        from tinydb.executor.write import (
            CreateTable,
            Delete,
            DropTable,
            Insert,
            Update,
        )

        return isinstance(
            op, (CreateTable, DropTable, Insert, Update, Delete)
        )

    @staticmethod
    def _consume_write(op: Operator) -> int:
        # Write operators do not stream rows; we just drive open/next/close.
        # The rows_affected attribute is set during open(); we read it here.
        affected = getattr(op, "rows_affected", 0)
        # Drive the operator to exhaustion so close() releases resources.
        while op.next() is not None:
            pass
        return affected

    @staticmethod
    def _stringify_row(row: Row) -> list[str]:
        return [_format_value(v) for v in row.values.values()]


def _format_value(v: Value) -> str:
    """Render a Value as a string for CLI / ResultSet output."""
    if v.is_null:
        return ""
    if v.tag is Tag.INT:
        return str(v.payload)
    if v.tag is Tag.FLOAT:
        f = float(v.payload)
        if f == int(f):
            return str(int(f))
        return repr(f)
    if v.tag is Tag.TEXT:
        return str(v.payload)
    if v.tag is Tag.BOOL:
        return "TRUE" if v.payload else "FALSE"
    if v.tag in (Tag.VARCHAR, Tag.CHAR, Tag.DECIMAL):
        # String-backed tags. CHAR keeps its right-padded spaces — do not
        # strip. DECIMAL is already a canonical string; do not reformat.
        return str(v.payload)
    if v.tag is Tag.SMALLINT or v.tag is Tag.BIGINT:
        return str(v.payload)
    if v.tag is Tag.DATE:
        return (date(1970, 1, 1) + timedelta(days=v.payload)).isoformat()
    if v.tag is Tag.TIME:
        secs = int(v.payload)
        return time(secs // 3600, (secs % 3600) // 60, secs % 60).isoformat()
    if v.tag is Tag.TIMESTAMP:
        return datetime.fromtimestamp(int(v.payload), tz=timezone.utc).isoformat()
    return repr(v.payload)
