"""In-memory buffer pool with LRU eviction and dirty-page tracking.

The pool keeps up to ``capacity`` pages resident. Pages are pinned while
in use so they cannot be evicted underneath the caller; ``unpin_page``
releases the pin. Eviction of a dirty page flushes it to disk first.

LRU order is tracked with ``collections.OrderedDict``; touching a page
moves it to the end. Eviction picks the first unpinned entry from the
front.

The pool can be wired to a ``flush_hook`` (e.g.
``TransactionManager.flush_page``) so that any write-through also
records the page in the WAL. This is required for durability — without
it, dirty pages would land on disk without a WAL trail, and recovery
on reopen would silently miss those mutations.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Callable, Optional

from tinydb.errors import StorageError
from tinydb.storage.disk import DiskManager
from tinydb.storage.page import Page


class BufferPool:
    def __init__(
        self,
        disk: DiskManager,
        capacity: int = 64,
        *,
        flush_hook: Optional[Callable[[Page], None]] = None,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self.disk = disk
        self.capacity = capacity
        # Optional callback invoked before a page is written to disk.
        # Use this to route writes through the WAL.
        self._flush_hook = flush_hook
        # page_id -> Page; insertion order = LRU order (oldest at front).
        self._pages: OrderedDict[int, Page] = OrderedDict()
        # page_id -> pin count
        self._pins: dict[int, int] = {}

    # ---- public API -------------------------------------------------------

    def fetch_page(self, page_id: int) -> Page:
        """Return the cached page for ``page_id``, loading from disk if needed.

        ``fetch_page`` returns the page with pin count = 1; callers must
        pair each ``fetch_page`` with an ``unpin_page`` (which decrements
        the pin count) once they are done with the page.
        """
        page = self._pages.get(page_id)
        if page is not None:
            self._pages.move_to_end(page_id)
            self._pins[page_id] = self._pins.get(page_id, 0) + 1
            return page
        # Load from disk; evict if necessary.
        if len(self._pages) >= self.capacity:
            self._evict_one()
        page = self.disk.read_page(page_id)
        self._pages[page_id] = page
        self._pins[page_id] = 1
        return page

    def register_page(self, page: Page) -> None:
        """Insert a freshly-allocated ``page`` into the pool with pin=1.

        Used after ``FreeList.allocate`` to make the new page eligible for
        eviction and (when dirty) flush. The page is assumed to already
        be on disk; we only track it in memory.
        """
        if page.page_id in self._pages:
            self._pages.move_to_end(page.page_id)
            self._pins[page.page_id] = self._pins.get(page.page_id, 0) + 1
            return
        if len(self._pages) >= self.capacity:
            self._evict_one()
        self._pages[page.page_id] = page
        self._pins[page.page_id] = 1

    def unpin_page(self, page_id: int, dirty: bool = False) -> None:
        if page_id not in self._pages:
            raise StorageError(f"unpin_page: page {page_id} not in pool")
        if self._pins[page_id] <= 0:
            raise StorageError(f"unpin_page: page {page_id} not pinned")
        if dirty:
            self._pages[page_id].dirty = True
        self._pins[page_id] -= 1

    def pin_count(self, page_id: int) -> int:
        return self._pins.get(page_id, 0)

    def is_dirty(self, page_id: int) -> bool:
        page = self._pages.get(page_id)
        return bool(page and page.dirty)

    def flush_all(self) -> None:
        """Write every dirty page to disk via the flush_hook (if any)."""
        for page_id, page in self._pages.items():
            if page.dirty:
                self._flush_via_hook(page)
                page.dirty = False

    def flush_page(self, page_id: int) -> None:
        page = self._pages.get(page_id)
        if page is None:
            raise StorageError(f"flush_page: page {page_id} not in pool")
        if page.dirty:
            self._flush_via_hook(page)
            page.dirty = False

    def discard_page(self, page_id: int) -> None:
        """Drop a page from the pool without flushing.

        Use when a page has been freed / logically replaced on disk by
        a layer below the pool (e.g. ``FreeList.free`` rewrites the
        page as FREE) — the cached copy would resurrect the old data
        if it survived eviction.
        """
        self._pages.pop(page_id, None)
        self._pins.pop(page_id, None)

    def size(self) -> int:
        return len(self._pages)

    # ---- internals --------------------------------------------------------

    def _flush_via_hook(self, page: Page) -> None:
        if self._flush_hook is not None:
            self._flush_hook(page)
        else:
            self.disk.write_page(page)

    def _evict_one(self) -> None:
        """Evict the least-recently-used UNPINNED page; flush if dirty.

        Raises StorageError if every cached page is pinned.
        """
        for page_id in self._pages:
            if self._pins.get(page_id, 0) == 0:
                page = self._pages.pop(page_id)
                self._pins.pop(page_id, None)
                if page.dirty:
                    self._flush_via_hook(page)
                return
        raise StorageError("buffer pool full: all pages are pinned")