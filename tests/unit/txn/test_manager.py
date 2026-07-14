"""Tests for TransactionManager: begin/commit/rollback/recovery/checkpoint.

These exercise the lifecycle and the rollback's pre-image capture, which
relies on the Page header being in sync with the in-memory attributes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tinydb.errors import TransactionError
from tinydb.storage.buffer import BufferPool
from tinydb.storage.disk import DiskManager
from tinydb.storage.page import PAGE_HEADER_SIZE, Page, PageType
from tinydb.txn.manager import TransactionManager


def _mgr(tmp_path: Path) -> TransactionManager:
    db = tmp_path / "tmgr.db"
    wal = tmp_path / "tmgr.wal"
    disk = DiskManager(db)
    disk.open()
    pool = BufferPool(disk, capacity=8)
    return TransactionManager(disk, pool, wal)


class TestBeginCommit:
    def test_begin_returns_strictly_increasing_ids(self, tmp_path):
        m = _mgr(tmp_path)
        m.open()
        try:
            a = m.begin()
            m.commit()
            b = m.begin()
            m.commit()
            assert b == a + 1
        finally:
            m.close()

    def test_nested_begin_raises(self, tmp_path):
        m = _mgr(tmp_path)
        m.open()
        try:
            m.begin()
            with pytest.raises(TransactionError):
                m.begin()
            m.commit()
        finally:
            m.close()

    def test_commit_outside_txn_raises(self, tmp_path):
        m = _mgr(tmp_path)
        m.open()
        try:
            with pytest.raises(TransactionError):
                m.commit()
        finally:
            m.close()

    def test_rollback_outside_txn_raises(self, tmp_path):
        m = _mgr(tmp_path)
        m.open()
        try:
            with pytest.raises(TransactionError):
                m.rollback()
        finally:
            m.close()

    def test_in_transaction_property(self, tmp_path):
        m = _mgr(tmp_path)
        m.open()
        try:
            assert not m.in_transaction
            m.begin()
            assert m.in_transaction
            m.commit()
            assert not m.in_transaction
        finally:
            m.close()


class TestLogPageWrite:
    def test_no_txn_is_noop(self, tmp_path):
        m = _mgr(tmp_path)
        m.open()
        try:
            page = Page.fresh(1, PageType.HEAP)
            m.log_page_write(page)
            assert m._undo == {}
        finally:
            m.close()

    def test_undo_log_records_pre_image(self, tmp_path):
        m = _mgr(tmp_path)
        m.open()
        try:
            m.begin()
            page = Page.fresh(1, PageType.HEAP)
            page.free_offset = 50
            m.log_page_write(page)
            assert 1 in m._undo
            # Stored bytes must match the page header at the time of capture.
            stored = m._undo[1][0][1]
            assert stored[3:5] == b"\x32\x00"  # 50 in little-endian u16
        finally:
            m.close()

    def test_pre_image_captures_after_attribute_mutation(self, tmp_path):
        """Regression: pre-image must reflect CURRENT header bytes, not stale."""
        m = _mgr(tmp_path)
        m.open()
        try:
            m.begin()
            page = Page.fresh(1, PageType.HEAP)
            page.append_payload(b"hello")  # free_offset -> PAGE_HEADER_SIZE + 5
            m.log_page_write(page)
            stored = m._undo[1][0][1]
            assert stored[3:5] == page.data[3:5]
        finally:
            m.close()


class TestRollback:
    def test_rollback_restores_page_bytes(self, tmp_path):
        """The core regression test for the rollback header-sync bug."""
        m = _mgr(tmp_path)
        m.open()
        try:
            # Allocate a page and put it in the pool with some prior state.
            page = Page.fresh(1, PageType.HEAP)
            page.append_payload(b"original")
            m.pool.register_page(page)
            assert page.free_offset == PAGE_HEADER_SIZE + 8

            m.begin()
            # Log pre-image BEFORE mutation, mirroring production usage.
            m.log_page_write(page)
            page.append_payload(b"-modified")
            assert page.free_offset == PAGE_HEADER_SIZE + 17

            m.rollback()

            # Re-fetch from pool — page should be restored to original state.
            p = m.pool.fetch_page(1)
            try:
                assert p.free_offset == PAGE_HEADER_SIZE + 8
                assert bytes(p.data)[PAGE_HEADER_SIZE:PAGE_HEADER_SIZE + 8] == b"original"
            finally:
                m.pool.unpin_page(1)
        finally:
            m.close()

    def test_rollback_multiple_writes_in_reverse_order(self, tmp_path):
        m = _mgr(tmp_path)
        m.open()
        try:
            page = Page.fresh(1, PageType.HEAP)
            m.pool.register_page(page)

            m.begin()
            m.log_page_write(page)
            page.append_payload(b"abc")
            m.log_page_write(page)
            page.append_payload(b"def")
            m.log_page_write(page)
            page.append_payload(b"ghi")
            assert page.free_offset == PAGE_HEADER_SIZE + 9

            m.rollback()

            p = m.pool.fetch_page(1)
            try:
                assert p.free_offset == PAGE_HEADER_SIZE
                # The page payload is zeros (not b"") because no rows were written.
                assert bytes(p.data)[PAGE_HEADER_SIZE:PAGE_HEADER_SIZE + 3] == b"\x00\x00\x00"
            finally:
                m.pool.unpin_page(1)
        finally:
            m.close()

    def test_rollback_with_no_writes_is_noop_on_pages(self, tmp_path):
        m = _mgr(tmp_path)
        m.open()
        try:
            m.begin()
            m.rollback()
            assert not m.in_transaction
        finally:
            m.close()

    def test_rollback_persists_restored_preimage_to_disk(self, tmp_path):
        """After ROLLBACK the restored pre-image must reach disk, not just
        memory. Otherwise a crash before the next eviction leaks the
        uncommitted post-image to the data file — recovery sees ABORT and
        cannot repair it.

        Setup: put a known pre-image on disk (page with payload b'original'),
        mutate it through a transaction, force-flush that post-image to disk
        mid-transaction, then ROLLBACK. We must NOT call flush_all after
        rollback or the test would pass trivially.
        """
        m = _mgr(tmp_path)
        m.open()
        try:
            page = Page.fresh(1, PageType.HEAP)
            page.append_payload(b"original")
            page._write_header()
            m.pool.register_page(page)
            m.pool.discard_page(1)  # ensure on-disk bytes are written
            # Force the pre-image to disk before any txn.
            from tinydb.storage.disk import DiskManager
            m.disk.write_page(page)
            m.disk.sync()

            # Start a txn and mutate the page (post-image).
            m.begin()
            m.log_page_write(page)
            page.append_payload(b"-modified")
            page._write_header()
            # Force the post-image to disk mid-txn (simulates eviction).
            m.disk.write_page(page)
            m.disk.sync()

            # ROLLBACK must restore the pre-image AND get it to disk.
            m.rollback()

            # Read disk directly (bypass pool cache). Slice long enough to
            # include both pre- and post-image portions so we can tell
            # whether the uncommitted '-modified' tail leaked to disk.
            raw = m.disk.read_page(1)
            payload = bytes(raw.data)[PAGE_HEADER_SIZE:PAGE_HEADER_SIZE + 17]
            assert payload == b"original\x00\x00\x00\x00\x00\x00\x00\x00\x00", (
                f"disk payload after rollback was {payload!r}; pre-image not persisted"
            )
        finally:
            m.close()

    def test_rollback_persists_multiple_pages_to_disk(self, tmp_path):
        """When a transaction touches several pages, rollback must persist
        every restored pre-image, not just the first one.
        """
        m = _mgr(tmp_path)
        m.open()
        try:
            # Three pages with distinct pre-images.
            pages = []
            for pid in (1, 2, 3):
                p = Page.fresh(pid, PageType.HEAP)
                p.append_payload(f"page{pid}-orig".encode())
                p._write_header()
                m.pool.register_page(p)
                pages.append(p)
            m.pool.discard_page(1)
            m.pool.discard_page(2)
            m.pool.discard_page(3)
            for p in pages:
                m.disk.write_page(p)
            m.disk.sync()

            m.begin()
            for pid, p in enumerate(pages, start=1):
                m.log_page_write(p)
                p.append_payload(b"-MOD")
                p._write_header()
                m.disk.write_page(p)  # post-image to disk mid-txn
            m.disk.sync()

            m.rollback()

            # All three pages on disk must show pre-image, no '-MOD' tail.
            for pid in (1, 2, 3):
                raw = m.disk.read_page(pid)
                # Slice is exactly the pre-image length (10 bytes); after
                # rollback the freed tail must be zero, not '-MOD'.
                payload = bytes(raw.data)[PAGE_HEADER_SIZE:PAGE_HEADER_SIZE + 10]
                assert payload == f"page{pid}-orig".encode(), (
                    f"page {pid} after rollback shows {payload!r}"
                )
        finally:
            m.close()

    def test_rollback_preimage_survives_close_reopen(self, tmp_path):
        """End-to-end durability: after ROLLBACK, closing and reopening
        the manager must show the pre-image state, never the uncommitted
        post-image.
        """
        m = _mgr(tmp_path)
        m.open()
        try:
            page = Page.fresh(1, PageType.HEAP)
            page.append_payload(b"committed-state")
            page._write_header()
            m.pool.register_page(page)
            m.pool.discard_page(1)
            m.disk.write_page(page)
            m.disk.sync()

            m.begin()
            m.log_page_write(page)
            page.append_payload(b"-NOT-COMMITTED")
            page._write_header()
            m.disk.write_page(page)
            m.disk.sync()
            m.rollback()
        finally:
            m.close()

        # Reopen and read the same page directly from disk.
        m2 = _mgr(tmp_path)
        m2.open()
        try:
            raw = m2.disk.read_page(1)
            payload = bytes(raw.data)[PAGE_HEADER_SIZE:PAGE_HEADER_SIZE + 30]
            assert payload.startswith(b"committed-state"), (
                f"reopen showed {payload!r}; rollback post-image leaked to disk"
            )
            assert b"NOT-COMMITTED" not in payload
        finally:
            m2.close()


class TestCheckpoint:
    def test_checkpoint_outside_txn_ok(self, tmp_path):
        m = _mgr(tmp_path)
        m.open()
        try:
            m.checkpoint()  # no exception
        finally:
            m.close()

    def test_checkpoint_inside_txn_raises(self, tmp_path):
        m = _mgr(tmp_path)
        m.open()
        try:
            m.begin()
            with pytest.raises(TransactionError):
                m.checkpoint()
            m.rollback()
        finally:
            m.close()


class TestCloseWithOpenTxn:
    def test_close_rolls_back_open_transaction(self, tmp_path):
        m = _mgr(tmp_path)
        m.open()
        page = Page.fresh(1, PageType.HEAP)
        m.pool.register_page(page)
        try:
            m.begin()
            page.append_payload(b"uncommitted")
            m.log_page_write(page)
        finally:
            m.close()
        # After close, no txn should be active.
        assert not m.in_transaction