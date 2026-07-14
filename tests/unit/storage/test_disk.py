"""Tests for DiskManager: file open/create, page read/write, header validation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tinydb.errors import StorageError
from tinydb.storage.disk import DiskManager
from tinydb.storage.page import (
    MAGIC,
    PAGE_HEADER_SIZE,
    PAGE_SIZE,
    FileHeader,
    Page,
    PageType,
)


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


class TestOpenCreate:
    def test_create_new_file_writes_header(self, db_file: Path):
        dm = DiskManager(db_file)
        dm.open()
        try:
            assert db_file.exists()
            # File starts with PAGE_SIZE bytes (just the header page).
            assert db_file.stat().st_size == PAGE_SIZE
            header = dm.read_header()
            assert header.magic == MAGIC
            assert header.version == 1
            assert header.page_count == 1
            assert header.catalog_root_page == 0
            assert header.free_list_head == 0
        finally:
            dm.close()

    def test_open_existing_file_validates_header(self, db_file: Path):
        # Create first.
        dm = DiskManager(db_file)
        dm.open()
        dm.close()

        # Reopen and verify.
        dm2 = DiskManager(db_file)
        dm2.open()
        try:
            assert dm2.read_header().magic == MAGIC
        finally:
            dm2.close()

    def test_open_invalid_magic_raises(self, db_file: Path):
        db_file.write_bytes(b"NOPENOPE" + b"\x00" * (PAGE_SIZE - 8))
        dm = DiskManager(db_file)
        with pytest.raises(StorageError):
            dm.open()

    def test_open_already_open_raises(self, db_file: Path):
        dm = DiskManager(db_file)
        dm.open()
        try:
            with pytest.raises(StorageError):
                dm.open()
        finally:
            dm.close()

    def test_close_then_operations_raise(self, db_file: Path):
        dm = DiskManager(db_file)
        dm.open()
        dm.close()
        with pytest.raises(StorageError):
            dm.read_header()


class TestReadWritePages:
    def test_allocate_blank_page_grows_file(self, db_file: Path):
        dm = DiskManager(db_file)
        dm.open()
        try:
            page = dm.allocate_blank_page(1, PageType.HEAP)
            assert page.page_id == 1
            assert page.page_type is PageType.HEAP
            assert dm.num_pages() == 2
            assert db_file.stat().st_size == 2 * PAGE_SIZE
        finally:
            dm.close()

    def test_round_trip_page(self, db_file: Path):
        dm = DiskManager(db_file)
        dm.open()
        try:
            p = dm.allocate_blank_page(1, PageType.HEAP)
            p.append_payload(b"hello world")
            dm.write_page(p)

            q = dm.read_page(1)
            assert q.page_id == 1
            assert q.page_type is PageType.HEAP
            assert bytes(q.data)[PAGE_HEADER_SIZE:PAGE_HEADER_SIZE + 11] == b"hello world"
        finally:
            dm.close()

    def test_read_out_of_range_page_raises(self, db_file: Path):
        dm = DiskManager(db_file)
        dm.open()
        try:
            with pytest.raises(StorageError):
                dm.read_page(99)
        finally:
            dm.close()

    def test_write_page_zero_raises(self, db_file: Path):
        dm = DiskManager(db_file)
        dm.open()
        try:
            p = Page.fresh(0, PageType.HEAP)
            with pytest.raises(StorageError):
                dm.write_page(p)
        finally:
            dm.close()

    def test_write_non_contiguous_raises(self, db_file: Path):
        dm = DiskManager(db_file)
        dm.open()
        try:
            p = Page.fresh(5, PageType.HEAP)
            with pytest.raises(StorageError):
                dm.write_page(p)
        finally:
            dm.close()

    def test_reopen_preserves_writes(self, db_file: Path):
        dm = DiskManager(db_file)
        dm.open()
        try:
            p = dm.allocate_blank_page(1, PageType.HEAP)
            p.append_payload(b"survives restart")
            dm.write_page(p)
        finally:
            dm.close()

        dm2 = DiskManager(db_file)
        dm2.open()
        try:
            q = dm2.read_page(1)
            assert bytes(q.data)[PAGE_HEADER_SIZE:PAGE_HEADER_SIZE + 16] == b"survives restart"
        finally:
            dm2.close()


class TestHeaderUpdates:
    def test_write_header_updates_on_disk(self, db_file: Path):
        dm = DiskManager(db_file)
        dm.open()
        try:
            h = dm.read_header()
            h.catalog_root_page = 42
            h.free_list_head = 7
            dm.write_header(h)

            h2 = dm.read_header()
            assert h2.catalog_root_page == 42
            assert h2.free_list_head == 7
        finally:
            dm.close()

    def test_write_header_rejects_bad_magic(self, db_file: Path):
        dm = DiskManager(db_file)
        dm.open()
        try:
            bad = FileHeader(magic=b"NOTMAGIC!")
            with pytest.raises(StorageError):
                dm.write_header(bad)
        finally:
            dm.close()