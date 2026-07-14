"""Tests for Page and FileHeader (in-memory structs)."""

from __future__ import annotations

import struct

import pytest

from tinydb.storage.page import (
    MAGIC,
    PAGE_HEADER_SIZE,
    PAGE_SIZE,
    VERSION,
    FileHeader,
    Page,
    PageType,
)


class TestFileHeaderRoundTrip:
    def test_pack_unpack_defaults(self):
        h = FileHeader()
        raw = h.pack()
        assert len(raw) == PAGE_SIZE
        out = FileHeader.unpack(raw)
        assert out == h

    def test_pack_unpack_modified(self):
        h = FileHeader(
            catalog_root_page=7,
            free_list_head=42,
            page_count=100,
        )
        out = FileHeader.unpack(h.pack())
        assert out.catalog_root_page == 7
        assert out.free_list_head == 42
        assert out.page_count == 100

    def test_pack_zero_pads_to_page_size(self):
        h = FileHeader()
        raw = h.pack()
        # Everything past the first 32 bytes must be zero.
        assert raw[32:] == b"\x00" * (PAGE_SIZE - 32)

    def test_validate_default(self):
        FileHeader().validate()  # no exception

    def test_validate_rejects_wrong_magic(self):
        from tinydb.errors import StorageError

        h = FileHeader(magic=b"NOPENOPE")
        with pytest.raises(StorageError):
            h.validate()

    def test_validate_rejects_wrong_version(self):
        from tinydb.errors import StorageError

        h = FileHeader(version=999)
        with pytest.raises(StorageError):
            h.validate()

    def test_current_version_is_2(self):
        """Catalog format bumped: ColumnMeta carries optional params.

        Old DBs (VERSION=1) get rejected by validate() at open time.
        """
        assert VERSION == 2

    def test_old_version_rejected_by_validate(self):
        """An old DB (VERSION=1) cannot be opened with the new code."""
        from tinydb.errors import StorageError

        h = FileHeader(version=1)
        with pytest.raises(StorageError) as excinfo:
            h.validate()
        assert "version" in str(excinfo.value).lower()

    def test_unpack_rejects_short_buffer(self):
        with pytest.raises(ValueError):
            FileHeader.unpack(b"\x00" * 16)


class TestPageRoundTrip:
    def test_fresh_page_is_page_size(self):
        p = Page.fresh(1, PageType.HEAP)
        assert len(p.data) == PAGE_SIZE

    def test_fresh_page_header_round_trip(self):
        p = Page.fresh(7, PageType.BTREE_LEAF)
        p.num_slots = 3
        p.free_offset = 100
        p.next = 42
        p.prev = 24
        p._write_header()
        q = Page.from_bytes(7, bytes(p.data))
        assert q.page_type is PageType.BTREE_LEAF
        assert q.num_slots == 3
        assert q.free_offset == 100
        assert q.next == 42
        assert q.prev == 24

    def test_payload_view_excludes_header(self):
        p = Page.fresh(1, PageType.HEAP)
        view = p.payload_view()
        assert len(view) == PAGE_SIZE - PAGE_HEADER_SIZE

    def test_append_payload_advances_offset(self):
        p = Page.fresh(1, PageType.HEAP)
        offset = p.append_payload(b"hello")
        assert offset == PAGE_HEADER_SIZE
        assert p.free_offset == PAGE_HEADER_SIZE + 5
        assert bytes(p.data)[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + 5] == b"hello"

    def test_append_payload_marks_dirty(self):
        p = Page.fresh(1, PageType.HEAP)
        assert not p.dirty
        p.append_payload(b"x")
        assert p.dirty

    def test_append_payload_overflow(self):
        p = Page.fresh(1, PageType.HEAP)
        # PAGE_SIZE - PAGE_HEADER_SIZE bytes max
        with pytest.raises(ValueError):
            p.append_payload(b"\x00" * (PAGE_SIZE - PAGE_HEADER_SIZE + 1))

    def test_remaining_space(self):
        p = Page.fresh(1, PageType.HEAP)
        assert p.remaining_space() == PAGE_SIZE - PAGE_HEADER_SIZE
        p.append_payload(b"12345")
        assert p.remaining_space() == PAGE_SIZE - PAGE_HEADER_SIZE - 5

    def test_from_bytes_rejects_wrong_size(self):
        with pytest.raises(ValueError):
            Page.from_bytes(1, b"\x00" * 100)


class TestHeaderBytes:
    """Verify the in-page header bytes are at the documented offsets."""

    def test_page_type_at_offset_0(self):
        p = Page.fresh(2, PageType.BTREE_INTERNAL)
        (pt,) = struct.unpack_from("<B", p.data, 0)
        assert pt == PageType.BTREE_INTERNAL

    def test_num_slots_at_offset_1(self):
        p = Page.fresh(2, PageType.HEAP)
        struct.pack_into("<H", p.data, 1, 99)
        q = Page.from_bytes(2, bytes(p.data))
        assert q.num_slots == 99

    def test_free_offset_at_offset_3(self):
        p = Page.fresh(2, PageType.HEAP)
        struct.pack_into("<H", p.data, 3, 200)
        q = Page.from_bytes(2, bytes(p.data))
        assert q.free_offset == 200

    def test_next_at_offset_5(self):
        p = Page.fresh(2, PageType.HEAP)
        struct.pack_into("<I", p.data, 5, 12345)
        q = Page.from_bytes(2, bytes(p.data))
        assert q.next == 12345

    def test_prev_at_offset_9(self):
        p = Page.fresh(2, PageType.HEAP)
        struct.pack_into("<I", p.data, 9, 67890)
        q = Page.from_bytes(2, bytes(p.data))
        assert q.prev == 67890