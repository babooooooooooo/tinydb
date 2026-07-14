"""Tests for FreeList: allocation, free, reuse."""

from __future__ import annotations

from pathlib import Path

import pytest

from tinydb.errors import StorageError
from tinydb.storage.disk import DiskManager
from tinydb.storage.freelist import FreeList
from tinydb.storage.page import PageType


@pytest.fixture
def env(tmp_path: Path):
    dm = DiskManager(tmp_path / "free.db")
    dm.open()
    fl = FreeList(dm)
    yield dm, fl
    dm.close()


class TestAllocate:
    def test_first_allocate_returns_page_1(self, env):
        dm, fl = env
        p = fl.allocate()
        assert p.page_id == 1
        # Header should now reflect 2 pages total.
        h = dm.read_header()
        assert h.page_count == 2
        assert h.free_list_head == 0

    def test_consecutive_allocates_grow_file(self, env):
        dm, fl = env
        ids = [fl.allocate().page_id for _ in range(5)]
        assert ids == [1, 2, 3, 4, 5]
        assert dm.read_header().page_count == 6

    def test_allocated_page_has_correct_type(self, env):
        _, fl = env
        p = fl.allocate(PageType.BTREE_LEAF)
        assert p.page_type is PageType.BTREE_LEAF


class TestFreeAndReuse:
    def test_free_pushes_onto_list(self, env):
        dm, fl = env
        fl.allocate()  # page 1
        fl.allocate()  # page 2
        fl.allocate()  # page 3
        fl.free(2)
        h = dm.read_header()
        assert h.free_list_head == 2
        assert h.page_count == 4  # file did not shrink

    def test_allocate_reuses_freed_page(self, env):
        dm, fl = env
        fl.allocate()  # 1
        fl.allocate()  # 2
        fl.allocate()  # 3
        fl.free(2)
        reused = fl.allocate()
        assert reused.page_id == 2
        # And the free list is empty now.
        assert dm.read_header().free_list_head == 0

    def test_reuse_preserves_lifo_order(self, env):
        _, fl = env
        for _ in range(3):
            fl.allocate()
        fl.free(1)
        fl.free(3)
        # LIFO: most recently freed is allocated first.
        assert fl.allocate().page_id == 3
        assert fl.allocate().page_id == 1

    def test_reused_page_is_fresh(self, env):
        _, fl = env
        fl.allocate().append_payload  # placeholder, will use below
        first = fl.allocate()
        first.append_payload(b"will-be-discarded")
        fl.free(first.page_id)
        reused = fl.allocate()
        # The reused page should not carry the previous payload.
        assert reused.free_offset == 13  # PAGE_HEADER_SIZE; no payload written
        assert reused.page_type is PageType.HEAP

    def test_free_header_raises(self, env):
        _, fl = env
        with pytest.raises(StorageError):
            fl.free(0)


class TestHeadAndCount:
    def test_head_initially_zero(self, env):
        _, fl = env
        assert fl.head() == 0

    def test_count_pages_initially_one(self, env):
        _, fl = env
        assert fl.count_pages() == 1

    def test_count_grows_with_allocate(self, env):
        _, fl = env
        fl.allocate()
        fl.allocate()
        assert fl.count_pages() == 3