"""Table-scan operators: SeqScan and IndexScan."""

from __future__ import annotations

from tinydb.catalog.schema import TableMeta
from tinydb.executor.heap import iter_rows
from tinydb.executor.operator import Operator
from tinydb.executor.row import Row
from tinydb.index.btree import BPlusTree
from tinydb.storage.buffer import BufferPool
from tinydb.storage.page import PAGE_HEADER_SIZE, Page, PageType
from tinydb.types import Value
from tinydb.types.serialize import deserialize


class SeqScan(Operator):
    """Stream every row of ``table`` in insertion order.

    Pins each heap page in turn; unpins before moving to the next. A page
    can hold many rows; we iterate them with the heap decoder.
    """

    def __init__(
        self,
        pool: BufferPool,
        table: TableMeta,
        alias: str | None = None,
    ) -> None:
        self.pool = pool
        self.table = table
        self.alias = alias
        self._page_id: int = table.heap_first_page
        self._cursor_offset: int = PAGE_HEADER_SIZE
        self._data: bytes = b""
        self._page: Page | None = None
        self._column_names = tuple(c.name for c in table.columns)

    def open(self) -> None:
        if self._page_id != 0:
            self._page = self.pool.fetch_page(self._page_id)
            self._data = bytes(self._page.data)
            self._cursor_offset = PAGE_HEADER_SIZE

    def close(self) -> None:
        if self._page is not None:
            self.pool.unpin_page(self._page.page_id, dirty=self._page.dirty)
            self._page = None

    def next(self) -> Row | None:
        while True:
            if self._page is None:
                return None
            # Walk rows in the current page.
            if self._cursor_offset < self._page.free_offset:
                from tinydb.executor.heap import decode_row

                values, next_off, deleted = decode_row(self._data, self._cursor_offset)
                self._cursor_offset = next_off
                if deleted:
                    continue
                row = Row(dict(zip(self._column_names, values)))
                return row
            # Move to the next page in the heap chain.
            if self._page.next == 0:
                self.pool.unpin_page(self._page.page_id, dirty=self._page.dirty)
                self._page = None
                return None
            nxt = self._page.next
            self.pool.unpin_page(self._page.page_id, dirty=self._page.dirty)
            self._page = self.pool.fetch_page(nxt)
            self._data = bytes(self._page.data)
            self._cursor_offset = PAGE_HEADER_SIZE


class IndexScan(Operator):
    """Stream rows via a B+ tree index on a single column.

    The index maps column-value → row-id (an opaque int). For our heap
    storage we encode the row-id as a single u32:
    the lower 16 bits hold the page id (0 reserved for "no page") and
    the upper 16 bits hold the row offset within the page. Page size is
    4096 so the offset fits in 12 bits; the upper 4 bits of the offset
    half remain zero.
    """

    ROW_ID_PAGE_MASK = 0xFFFF
    ROW_ID_OFFSET_SHIFT = 16

    def __init__(
        self,
        pool: BufferPool,
        tree: BPlusTree,
        table: TableMeta,
        index_column: str,
    ) -> None:
        self.pool = pool
        self.tree = tree
        self.table = table
        self.index_column = index_column
        self._iter: list[tuple[Value, int]] | None = None
        self._column_names = tuple(c.name for c in table.columns)

    @staticmethod
    def encode_row_id(page_id: int, offset_in_page: int) -> int:
        if page_id < 0 or page_id > 0xFFFF:
            raise ValueError(f"page_id {page_id} out of range for row id")
        if offset_in_page < 0 or offset_in_page > 0xFFFF:
            raise ValueError(f"offset {offset_in_page} out of range for row id")
        return (offset_in_page << IndexScan.ROW_ID_OFFSET_SHIFT) | (
            page_id & IndexScan.ROW_ID_PAGE_MASK
        )

    @staticmethod
    def decode_row_id(row_id: int) -> tuple[int, int]:
        page_id = row_id & IndexScan.ROW_ID_PAGE_MASK
        offset = row_id >> IndexScan.ROW_ID_OFFSET_SHIFT
        return page_id, offset

    def open(self) -> None:
        # Default: full range scan.
        self._iter = iter(self.tree.range_scan(None, None))

    def close(self) -> None:
        self._iter = None

    def next(self) -> Row | None:
        if self._iter is None:
            return None
        for key, row_id in self._iter:
            page_id, offset = self.decode_row_id(row_id)
            page = self.pool.fetch_page(page_id)
            try:
                from tinydb.executor.heap import decode_row

                values, _, deleted = decode_row(bytes(page.data), offset)
            finally:
                self.pool.unpin_page(page_id, dirty=False)
            if deleted:
                continue
            return Row(dict(zip(self._column_names, values)))
        return None