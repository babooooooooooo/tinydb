"""Low-level disk I/O: file open/create, page read/write, atomic grow."""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

from tinydb.errors import StorageError
from tinydb.storage.page import (
    MAGIC,
    PAGE_SIZE,
    VERSION,
    FileHeader,
    Page,
    PageType,
)


class DiskManager:
    """Owns the open file handle and provides page-level read/write.

    Single-writer, single-process. Thread-/process-safety is out of scope.
    The file is page-aligned at all times (header page + N data pages).
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._fh: IO[bytes] | None = None

    # ---- lifecycle --------------------------------------------------------

    def open(self) -> None:
        """Open the file in read+write mode, creating it if absent.

        On create, an initial FileHeader page is written. On open of an
        existing file, the header is read and validated (magic + version).
        """
        if self._fh is not None:
            raise StorageError("disk manager already open")
        if self.path.exists():
            self._fh = self.path.open("r+b")
            self._fh.seek(0)
            header = FileHeader.unpack(self._fh.read(PAGE_SIZE))
            header.validate()
        else:
            self._fh = self.path.open("w+b")
            header = FileHeader()
            self._fh.write(header.pack())
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None

    @property
    def is_open(self) -> bool:
        return self._fh is not None

    # ---- header -----------------------------------------------------------

    def read_header(self) -> FileHeader:
        self._ensure_open()
        assert self._fh is not None
        self._fh.seek(0)
        return FileHeader.unpack(self._fh.read(PAGE_SIZE))

    def write_header(self, header: FileHeader) -> None:
        self._ensure_open()
        assert self._fh is not None
        header.validate()
        self._fh.seek(0)
        self._fh.write(header.pack())
        self._fh.flush()
        os.fsync(self._fh.fileno())

    # ---- pages ------------------------------------------------------------

    def num_pages(self) -> int:
        """Number of pages currently in the file (including the header page)."""
        self._ensure_open()
        assert self._fh is not None
        self._fh.seek(0, os.SEEK_END)
        end = self._fh.tell()
        return end // PAGE_SIZE

    def read_page(self, page_id: int) -> Page:
        self._ensure_open()
        if page_id < 0:
            raise StorageError(f"invalid page id {page_id}")
        n = self.num_pages()
        if page_id >= n:
            raise StorageError(f"page {page_id} out of range (file has {n} pages)")
        assert self._fh is not None
        self._fh.seek(page_id * PAGE_SIZE)
        raw = self._fh.read(PAGE_SIZE)
        if len(raw) != PAGE_SIZE:
            raise StorageError(f"short read on page {page_id}: got {len(raw)} bytes")
        return Page.from_bytes(page_id, raw)

    def write_page(self, page: Page) -> None:
        """Write ``page`` to disk. Auto-grows the file if the page id is past EOF.

        Caller is responsible for syncing the page header into ``page.data``
        (e.g. via ``page._write_header()``) before calling.
        """
        self._ensure_open()
        assert self._fh is not None
        if page.page_id == 0:
            raise StorageError("cannot overwrite the file header page as a data page")
        page._write_header()  # ensure in-page header is current
        target_offset = page.page_id * PAGE_SIZE
        self._fh.seek(0, os.SEEK_END)
        end = self._fh.tell()
        if target_offset > end:
            raise StorageError(
                f"non-contiguous write: page_id={page.page_id} but file ends at {end}"
            )
        if target_offset == end:
            # Extend the file by one page.
            self._fh.write(bytes(PAGE_SIZE))
        self._fh.seek(target_offset)
        self._fh.write(bytes(page.data))
        self._fh.flush()

    def allocate_blank_page(self, page_id: int, page_type: PageType) -> Page:
        """Allocate a fresh page (in-memory + on-disk) with the given id and type."""
        page = Page.fresh(page_id, page_type)
        self.write_page(page)
        return page

    def sync(self) -> None:
        self._ensure_open()
        assert self._fh is not None
        self._fh.flush()
        os.fsync(self._fh.fileno())

    # ---- internals --------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._fh is None:
            raise StorageError("disk manager is not open")