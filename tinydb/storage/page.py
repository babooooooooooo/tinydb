"""Page-level on-disk structures: constants, FileHeader, Page container.

Disk layout (file is a sequence of fixed-size pages):

    [Page 0: FileHeader (4096 bytes)] [Page 1] [Page 2] ...

Each non-header page begins with a small in-page header so the page is
self-describing:

    offset  size  field
    ------  ----  -----
    0       1     page_type  (PageType)
    1       2     num_slots  (u16)
    3       2     free_offset (u16, where the next byte can be written)
    5       4     next       (u32, semantics depend on page_type)
    9       4     prev       (u32, semantics depend on page_type)
    13      ...

For FREE pages the ``next`` field holds the next free page id.
For BTREE_LEAF pages ``next`` is the next leaf's page id (range-scan chain).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum


PAGE_SIZE: int = 4096
MAGIC: bytes = b"TINYDB\x00\x00"  # exactly 8 bytes
VERSION: int = 1

# FileHeader occupies exactly PAGE_SIZE bytes; only the first 32 are defined.
_HEADER_FORMAT = struct.Struct("<8sIIIIII")  # 32 bytes
assert _HEADER_FORMAT.size == 32, _HEADER_FORMAT.size

# In-page header for non-header pages: 13 bytes used.
_PAGE_HEADER_FORMAT = struct.Struct("<BHHII")
PAGE_HEADER_SIZE: int = _PAGE_HEADER_FORMAT.size
assert PAGE_HEADER_SIZE == 13, PAGE_HEADER_SIZE


class PageType(IntEnum):
    """On-disk page kind. Numeric values are the on-disk byte encoding."""

    FREE = 0
    CATALOG = 1
    HEAP = 2
    BTREE_INTERNAL = 3
    BTREE_LEAF = 4


@dataclass
class FileHeader:
    """In-memory representation of the 4 KiB header page (page 0)."""

    magic: bytes = MAGIC
    version: int = VERSION
    page_size: int = PAGE_SIZE
    page_count: int = 1  # at minimum the header page itself
    catalog_root_page: int = 0  # 0 = no catalog allocated yet
    free_list_head: int = 0  # 0 = empty free list

    def pack(self) -> bytes:
        """Serialize the header into a 4 KiB page (zero-padded)."""
        body = _HEADER_FORMAT.pack(
            self.magic,
            self.version,
            self.page_size,
            self.page_count,
            self.catalog_root_page,
            self.free_list_head,
            0,  # reserved (u32)
        )
        # Zero-pad to full PAGE_SIZE so the file is page-aligned on disk.
        return body + b"\x00" * (PAGE_SIZE - len(body))

    @classmethod
    def unpack(cls, data: bytes) -> "FileHeader":
        if len(data) < _HEADER_FORMAT.size:
            raise ValueError("header buffer too short")
        magic, version, page_size, page_count, catalog_root, free_head, _ = (
            _HEADER_FORMAT.unpack_from(data, 0)
        )
        return cls(
            magic=magic,
            version=version,
            page_size=page_size,
            page_count=page_count,
            catalog_root_page=catalog_root,
            free_list_head=free_head,
        )

    def validate(self) -> None:
        """Raise ValueError if the magic or version is unexpected.

        Importing the storage error lazily avoids a cycle (errors module
        doesn't need storage, but storage raises TinyDBError subclasses).
        """
        from tinydb.errors import StorageError

        if self.magic != MAGIC:
            raise StorageError(f"invalid magic: {self.magic!r}")
        if self.version != VERSION:
            raise StorageError(f"unsupported version: {self.version}")


@dataclass
class Page:
    """An in-memory page loaded from disk or freshly allocated.

    ``data`` is a full-PAGE_SIZE bytearray; the in-page header lives at
    the front. ``dirty`` is set by callers (or by BufferPool when a write
    happens) and is the signal to flush the page back to disk.

    The header fields (num_slots, free_offset, next, prev) are
    properties backed by private storage; their setters write through to
    ``data[0:13]`` so the on-disk bytes never drift from the in-memory
    attributes. Use ``_read_header`` (loading from disk) and the
    underlying private attrs when bypassing the sync.
    """

    page_id: int
    data: bytearray = field(default_factory=bytearray)
    dirty: bool = False
    # Private header storage. Public access goes through properties
    # below; mutations auto-sync to ``data``.
    _page_type: int = PageType.FREE
    _num_slots: int = 0
    _free_offset: int = PAGE_HEADER_SIZE
    _next: int = 0
    _prev: int = 0

    # ---- header field properties ------------------------------------------

    @property
    def page_type(self) -> PageType:
        return PageType(self._page_type)

    @page_type.setter
    def page_type(self, value: PageType) -> None:
        self._page_type = int(value)
        self._write_header()

    @property
    def num_slots(self) -> int:
        return self._num_slots

    @num_slots.setter
    def num_slots(self, value: int) -> None:
        self._num_slots = value
        self._write_header()

    @property
    def free_offset(self) -> int:
        return self._free_offset

    @free_offset.setter
    def free_offset(self, value: int) -> None:
        self._free_offset = value
        self._write_header()

    @property
    def next(self) -> int:
        return self._next

    @next.setter
    def next(self, value: int) -> None:
        self._next = value
        self._write_header()

    @property
    def prev(self) -> int:
        return self._prev

    @prev.setter
    def prev(self, value: int) -> None:
        self._prev = value
        self._write_header()

    # ---- factories --------------------------------------------------------

    @classmethod
    def fresh(cls, page_id: int, page_type: PageType = PageType.FREE) -> "Page":
        """Allocate a new in-memory page of PAGE_SIZE bytes with header zeroed."""
        p = cls(page_id=page_id)
        p._page_type = int(page_type)
        p.data = bytearray(PAGE_SIZE)
        p._write_header()
        return p

    @classmethod
    def from_bytes(cls, page_id: int, raw: bytes | bytearray) -> "Page":
        """Rehydrate a page from raw on-disk bytes; parses the in-page header."""
        if len(raw) != PAGE_SIZE:
            raise ValueError(f"page buffer must be {PAGE_SIZE} bytes, got {len(raw)}")
        p = cls(page_id=page_id, data=bytearray(raw))
        p._read_header()
        return p

    # ---- header sync ------------------------------------------------------

    def _write_header(self) -> None:
        _PAGE_HEADER_FORMAT.pack_into(
            self.data,
            0,
            self._page_type,
            self._num_slots,
            self._free_offset,
            self._next,
            self._prev,
        )

    def _read_header(self) -> None:
        pt, ns, fo, nxt, prv = _PAGE_HEADER_FORMAT.unpack_from(self.data, 0)
        # Set private storage directly to avoid re-triggering _write_header.
        self._page_type = pt
        self._num_slots = ns
        self._free_offset = fo
        self._next = nxt
        self._prev = prv

    # ---- payload access ---------------------------------------------------

    def payload_view(self) -> memoryview:
        """Read-only view of the payload (everything after the in-page header)."""
        return memoryview(self.data)[PAGE_HEADER_SIZE:]

    def remaining_space(self) -> int:
        return PAGE_SIZE - self.free_offset

    def append_payload(self, data: bytes) -> int:
        """Append ``data`` to the payload area; returns the offset written at."""
        offset = self.free_offset
        if offset + len(data) > PAGE_SIZE:
            raise ValueError("page full")
        self.data[offset : offset + len(data)] = data
        self.free_offset = offset + len(data)
        self.dirty = True
        return offset

    def mark_dirty(self) -> None:
        self.dirty = True