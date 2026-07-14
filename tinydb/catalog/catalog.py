"""Catalog: persistent metadata for tables, columns, indexes.

The catalog is serialized into one or more HEAP pages whose ids are
recorded in the FileHeader's ``catalog_root_page`` field. The first
catalog page is the head; additional pages form a singly-linked list
via each page's ``next`` field. Tables are delimited by an end marker
byte (``TableMeta.END_MARKER``).
"""

from __future__ import annotations

import struct

from tinydb.catalog.schema import ColumnMeta, IndexMeta, TableMeta
from tinydb.errors import ConstraintError, StorageError
from tinydb.storage.disk import DiskManager
from tinydb.storage.freelist import FreeList
from tinydb.storage.page import Page, PageType


class Catalog:
    """In-memory representation of the catalog, backed by persisted pages."""

    def __init__(
        self,
        disk: DiskManager,
        freelist: FreeList,
        txn: Optional["TransactionManager"] = None,
    ) -> None:
        self.disk = disk
        self.freelist = freelist
        self.txn = txn
        self._tables: dict[str, TableMeta] = {}
        # Persisted on close: ids of catalog pages in chain order.
        self._page_chain: list[int] = []

    def _write_header(self, header: FileHeader) -> None:
        """Route header writes through the txn manager so WAL records them."""
        if self.txn is not None:
            self.txn.flush_header(header)
        else:
            self.disk.write_header(header)

    # ---- lifecycle --------------------------------------------------------

    def load(self) -> None:
        """Read the catalog from disk into memory.

        Called by ``Database.open`` after the disk layer is initialized.
        If no catalog pages are recorded, the catalog is empty.
        """
        header = self.disk.read_header()
        head = header.catalog_root_page
        if head == 0:
            return
        # Walk the chain.
        page_id = head
        chain: list[int] = []
        visited = set()
        while page_id != 0:
            if page_id in visited:
                raise StorageError(f"catalog page chain loops at page {page_id}")
            visited.add(page_id)
            chain.append(page_id)
            page = self.disk.read_page(page_id)
            (next_id,) = struct.unpack_from("<I", page.data, 5)  # 'next' is at offset 5
            page_id = next_id
        self._page_chain = chain
        # Concatenate the payload of all catalog pages and parse.
        payload = b""
        for pid in chain:
            page = self.disk.read_page(pid)
            payload += bytes(page.data)[13:]  # skip in-page header
        offset = 0
        while offset < len(payload):
            (marker,) = struct.unpack_from("<B", payload, offset)
            if marker == 0:
                # Zero-fill padding at end of last page; stop.
                break
            if marker == TableMeta.END_MARKER:
                offset += 1
                continue
            # Backtrack one byte so TableMeta.unpack can read the marker itself.
            table, offset = TableMeta.unpack(payload, offset)
            self._tables[table.name] = table

    def save(self) -> None:
        """Serialize the catalog back to disk, replacing the previous chain."""
        # Build payload.
        payload = b"".join(t.pack() for t in self._tables.values())
        if not payload:
            # Empty catalog: free any old chain and clear the header pointer.
            for old in self._page_chain:
                try:
                    self.freelist.free(old)
                except StorageError:
                    pass
            self._page_chain = []
            header = self.disk.read_header()
            header.catalog_root_page = 0
            self._write_header(header)
            return
        # Pack into HEAP pages (4 KiB - 13 bytes payload each).
        per_page = 4096 - 13
        chunks = [payload[i : i + per_page] for i in range(0, len(payload), per_page)]
        # Allocate fresh catalog pages first (collect ids), then write linkage.
        new_chain: list[int] = []
        new_pages: list[Page] = []
        for chunk in chunks:
            page = self.freelist.allocate(PageType.CATALOG)
            page.data[13 : 13 + len(chunk)] = chunk
            page.free_offset = 13 + len(chunk)
            page.dirty = True
            new_chain.append(page.page_id)
            new_pages.append(page)
        # Now that all ids are known, link pages and write them.
        for i, page in enumerate(new_pages):
            page.next = new_chain[i + 1] if i + 1 < len(new_chain) else 0
            self.disk.write_page(page)
        # fsync the data file so the catalog bytes are durable BEFORE we
        # update the header (which is also fsync'd). Without this the
        # kernel could reorder: header lands pointing at a catalog
        # chain that never reached disk, and reopen sees an empty
        # catalog (silent data loss).
        self.disk.sync()
        # Free old catalog pages.
        for old in self._page_chain:
            try:
                self.freelist.free(old)
            except StorageError:
                pass
        # Update header.
        header = self.disk.read_header()
        header.catalog_root_page = new_chain[0]
        self._write_header(header)
        self._page_chain = new_chain

    # ---- queries ----------------------------------------------------------

    def has_table(self, name: str) -> bool:
        return name in self._tables

    def get_table(self, name: str) -> TableMeta:
        if name not in self._tables:
            raise StorageError(f"no such table: {name}")
        return self._tables[name]

    def list_tables(self) -> list[str]:
        return sorted(self._tables.keys())

    def iter_tables(self):
        return self._tables.values()

    # ---- mutations --------------------------------------------------------

    def create_table(self, name: str, columns: list[ColumnMeta]) -> TableMeta:
        if name in self._tables:
            raise ConstraintError(f"table {name!r} already exists")
        # Validate: no duplicate column names; at most one PRIMARY KEY.
        seen: set[str] = set()
        pk_count = 0
        for c in columns:
            if c.name in seen:
                raise ConstraintError(f"duplicate column {c.name!r} in CREATE TABLE {name!r}")
            seen.add(c.name)
            if c.is_primary_key:
                pk_count += 1
        if pk_count > 1:
            raise ConstraintError(f"table {name!r} has multiple PRIMARY KEY columns")
        meta = TableMeta(name=name, columns=tuple(columns))
        self._tables[name] = meta
        return meta

    def drop_table(self, name: str) -> None:
        if name not in self._tables:
            raise StorageError(f"no such table: {name}")
        meta = self._tables[name]
        # Walk the heap chain to collect page ids BEFORE removing the
        # metadata. If read_page raises mid-walk, the table stays in the
        # catalog so its pages remain reachable (no silent leak).
        heap_pages: list[int] = []
        page_id = meta.heap_first_page
        visited: set[int] = set()
        while page_id != 0:
            if page_id in visited:
                break
            visited.add(page_id)
            page = self.disk.read_page(page_id)
            heap_pages.append(page_id)
            page_id = page.next
        # Metadata removal and page frees only happen once the walk fully
        # succeeded above.
        del self._tables[name]
        for pid in heap_pages:
            try:
                self.freelist.free(pid)
            except StorageError:
                pass
        # Free the table's index roots (best effort).
        for idx in meta.indexes:
            if idx.root_page != 0:
                try:
                    self.freelist.free(idx.root_page)
                except StorageError:
                    pass

    def update_table(self, meta: TableMeta) -> None:
        """Replace the in-memory entry for ``meta.name`` (used by executor)."""
        self._tables[meta.name] = meta

    def add_index(self, table_name: str, index: IndexMeta) -> TableMeta:
        meta = self.get_table(table_name)
        if any(i.name == index.name for i in meta.indexes):
            raise ConstraintError(f"index {index.name!r} already exists")
        new = TableMeta(
            name=meta.name,
            columns=meta.columns,
            heap_first_page=meta.heap_first_page,
            heap_last_page=meta.heap_last_page,
            indexes=meta.indexes + (index,),
            row_count=meta.row_count,
        )
        self._tables[table_name] = new
        return new

    def drop_index(self, table_name: str, index_name: str) -> None:
        meta = self.get_table(table_name)
        new = TableMeta(
            name=meta.name,
            columns=meta.columns,
            heap_first_page=meta.heap_first_page,
            heap_last_page=meta.heap_last_page,
            indexes=tuple(i for i in meta.indexes if i.name != index_name),
            row_count=meta.row_count,
        )
        self._tables[table_name] = new