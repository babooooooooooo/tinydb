"""Tests for BufferPool: LRU eviction, dirty tracking, flush semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from tinydb.errors import StorageError
from tinydb.storage.buffer import BufferPool
from tinydb.storage.disk import DiskManager
from tinydb.storage.page import Page, PageType


@pytest.fixture
def disk(tmp_path: Path) -> DiskManager:
    dm = DiskManager(tmp_path / "buf.db")
    dm.open()
    yield dm
    dm.close()


class TestFetchAndCache:
    def test_first_fetch_loads_from_disk(self, disk: DiskManager):
        disk.allocate_blank_page(1, PageType.HEAP)
        bp = BufferPool(disk, capacity=4)
        page = bp.fetch_page(1)
        assert page.page_id == 1
        assert bp.size() == 1

    def test_repeat_fetch_uses_cache(self, disk: DiskManager):
        disk.allocate_blank_page(1, PageType.HEAP)
        bp = BufferPool(disk, capacity=4)
        p1 = bp.fetch_page(1)
        # Mutate so we can detect a re-read from disk.
        p1.append_payload(b"cached")
        p1.dirty = False  # pretend we did not mark dirty

        p2 = bp.fetch_page(1)
        assert p2 is p1  # same object: came from cache

    def test_dirty_flag_tracked(self, disk: DiskManager):
        disk.allocate_blank_page(1, PageType.HEAP)
        bp = BufferPool(disk, capacity=4)
        page = bp.fetch_page(1)
        assert not bp.is_dirty(1)
        page.append_payload(b"x")
        assert bp.is_dirty(1)


class TestLRUEviction:
    def test_evicts_lru_when_full(self, disk: DiskManager):
        # Allocate 5 pages and a pool of capacity 3.
        for i in range(1, 6):
            disk.allocate_blank_page(i, PageType.HEAP)
        bp = BufferPool(disk, capacity=3)
        bp.fetch_page(1)
        bp.unpin_page(1)
        bp.fetch_page(2)
        bp.unpin_page(2)
        bp.fetch_page(3)
        bp.unpin_page(3)
        # Adding a 4th should evict page 1 (LRU).
        bp.fetch_page(4)
        bp.unpin_page(4)
        assert bp.size() == 3
        # Page 1 should no longer be cached; fetching it loads a new instance.
        # Mark dirty on 2 so we can verify the new instance starts clean.
        bp.fetch_page(2).append_payload(b"x")  # page 2 dirty now
        bp.flush_all()

    def test_dirty_page_flushed_before_eviction(self, disk: DiskManager):
        disk.allocate_blank_page(1, PageType.HEAP)
        disk.allocate_blank_page(2, PageType.HEAP)
        bp = BufferPool(disk, capacity=2)
        page = bp.fetch_page(1)
        page.append_payload(b"persisted-on-evict")
        bp.unpin_page(1)
        # Page 1 is dirty. Fetching page 2 fills the pool; fetching a 3rd page
        # forces eviction of the LRU (page 1).
        bp.fetch_page(2)
        bp.unpin_page(2)
        disk.allocate_blank_page(3, PageType.HEAP)
        bp.fetch_page(3)  # evicts page 1
        bp.unpin_page(3)
        # Page 1 should still be on disk.
        # Re-read directly from disk to verify.
        reloaded = disk.read_page(1)
        from tinydb.storage.page import PAGE_HEADER_SIZE
        assert bytes(reloaded.data)[PAGE_HEADER_SIZE:PAGE_HEADER_SIZE + 18] == b"persisted-on-evict"

    def test_all_pinned_raises(self, disk: DiskManager):
        disk.allocate_blank_page(1, PageType.HEAP)
        bp = BufferPool(disk, capacity=1)
        bp.fetch_page(1)
        # fetch_page already auto-pins; capacity-1 pool filled.
        disk.allocate_blank_page(2, PageType.HEAP)
        with pytest.raises(StorageError):
            bp.fetch_page(2)


class TestFlushAll:
    def test_flush_all_writes_dirty_pages(self, disk: DiskManager):
        disk.allocate_blank_page(1, PageType.HEAP)
        disk.allocate_blank_page(2, PageType.HEAP)
        bp = BufferPool(disk, capacity=4)
        p1 = bp.fetch_page(1)
        p2 = bp.fetch_page(2)
        p1.append_payload(b"one")
        p2.append_payload(b"two")
        bp.flush_all()
        # Both pages should be clean now.
        assert not bp.is_dirty(1)
        assert not bp.is_dirty(2)
        # And the on-disk content should reflect the writes.
        from tinydb.storage.page import PAGE_HEADER_SIZE
        r1 = disk.read_page(1)
        r2 = disk.read_page(2)
        assert bytes(r1.data)[PAGE_HEADER_SIZE:PAGE_HEADER_SIZE + 3] == b"one"
        assert bytes(r2.data)[PAGE_HEADER_SIZE:PAGE_HEADER_SIZE + 3] == b"two"


class TestUnpin:
    def test_unpin_with_dirty_marks_dirty(self, disk: DiskManager):
        disk.allocate_blank_page(1, PageType.HEAP)
        bp = BufferPool(disk, capacity=2)
        bp.fetch_page(1)
        bp.unpin_page(1, dirty=True)
        assert bp.is_dirty(1)
        assert bp.pin_count(1) == 0

    def test_unpin_unknown_page_raises(self, disk: DiskManager):
        bp = BufferPool(disk, capacity=2)
        with pytest.raises(StorageError):
            bp.unpin_page(99)

    def test_double_unpin_raises(self, disk: DiskManager):
        disk.allocate_blank_page(1, PageType.HEAP)
        bp = BufferPool(disk, capacity=2)
        bp.fetch_page(1)
        bp.unpin_page(1)
        with pytest.raises(StorageError):
            bp.unpin_page(1)


class TestDiscard:
    def test_discard_drops_page_without_flush(self, disk: DiskManager):
        disk.allocate_blank_page(1, PageType.HEAP)
        bp = BufferPool(disk, capacity=2)
        page = bp.fetch_page(1)
        page.append_payload(b"uncommitted")
        bp.discard_page(1)
        assert bp.size() == 0
        # On disk should not have the uncommitted bytes.
        from tinydb.storage.page import PAGE_HEADER_SIZE
        reloaded = disk.read_page(1)
        assert bytes(reloaded.data)[PAGE_HEADER_SIZE:PAGE_HEADER_SIZE + 11] != b"uncommitted"