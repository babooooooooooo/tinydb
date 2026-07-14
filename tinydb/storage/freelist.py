"""Free-list management backed by the FileHeader's ``free_list_head``.

A free page's ``next`` field (in-page header) points to the next free
page, forming a singly-linked list whose head lives in FileHeader. When
the list is empty, allocating a new id extends the file by one page.

The freelist is wired to a ``BufferPool`` (when available) so freeing a
page also evicts any cached copy — otherwise the pool could later
overwrite the freshly-freed page with stale data.

When a ``TransactionManager`` is attached, ``free`` is deferred until
the transaction commits — this lets DROP TABLE inside a transaction be
undone cleanly. Outside a transaction the free is immediate.
"""

from __future__ import annotations

from typing import Optional

from tinydb.errors import StorageError
from tinydb.storage.buffer import BufferPool
from tinydb.storage.disk import DiskManager
from tinydb.storage.page import FileHeader, Page, PageType


class FreeList:
    """Allocator/deallocator of page ids.

    Operates directly on DiskManager so the catalog/executor layer can
    ask for fresh pages without thinking about file layout.
    """

    def __init__(
        self,
        disk: DiskManager,
        pool: Optional[BufferPool] = None,
        txn: Optional["TransactionManager"] = None,
    ) -> None:
        self.disk = disk
        self.pool = pool
        self.txn = txn

    def allocate(self, page_type: PageType = PageType.HEAP) -> Page:
        """Return a fresh page, either reused from the free list or appended."""
        header = self.disk.read_header()
        if header.free_list_head != 0:
            page_id = header.free_list_head
            page = self.disk.read_page(page_id)
            # The freed page's ``next`` field points to the next free page.
            new_head = page.next
            header.free_list_head = new_head
            self.disk.write_header(header)
            # Reinitialize the reused page in place and write it to disk
            # so the on-disk bytes match the in-memory state. Without
            # this write a crash between allocate and first mutation
            # would leave a stale FREE page on disk.
            new_page = Page.fresh(page_id, page_type)
            self.disk.write_page(new_page)
            # Drop any cached copy of this page so subsequent reads see
            # the freshly-written bytes, not whatever the pool had.
            if self.pool is not None:
                self.pool.discard_page(page_id)
            return new_page
        # Free list is empty: extend the file by one page.
        new_id = header.page_count
        header.page_count = new_id + 1
        self.disk.write_header(header)
        return self.disk.allocate_blank_page(new_id, page_type)

    def free(self, page_id: int) -> None:
        """Push ``page_id`` onto the front of the free list.

        If a transaction is active, the free is deferred until commit;
        on rollback the deferred list is discarded so the pages stay
        intact.
        """
        if page_id == 0:
            raise StorageError("cannot free the header page")
        if self.txn is not None and self.txn.in_transaction:
            self.txn.defer_free(page_id)
            return
        self._free_now(page_id)

    def _free_now(self, page_id: int) -> None:
        """Perform the actual free (called on commit or when no txn)."""
        header = self.disk.read_header()
        old_head = header.free_list_head
        # Rewrite the page in place as a FREE list node.
        page = Page.fresh(page_id, PageType.FREE)
        page.next = old_head
        page.dirty = True
        self.disk.write_page(page)
        # Drop any cached copy so the pool can't resurrect stale data.
        if self.pool is not None:
            self.pool.discard_page(page_id)
        header.free_list_head = page_id
        self.disk.write_header(header)

    def head(self) -> int:
        return self.disk.read_header().free_list_head

    def count_pages(self) -> int:
        return self.disk.read_header().page_count