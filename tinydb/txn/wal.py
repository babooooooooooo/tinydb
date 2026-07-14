"""Write-Ahead Log (WAL): durable record of page-level changes.

The WAL sits alongside the main database file (``<dbfile>.wal``) and
holds one record per change. Each record carries:

* the **LSN** (log sequence number): strictly increasing across the WAL,
  used by recovery to ignore already-applied records.
* the **page id** and the **full page bytes** (snapshot, not delta).
  Full-page logging keeps the writer simple — there is no need to
  thread before/after images through the executor. The trade-off is WAL
  size: each page-mutation writes a full 4 KiB page.
* the **txn id** that performed the change.
* a **commit flag**: a BEGIN record opens a txn; a COMMIT record marks
  it durable; if recovery encounters records without a matching COMMIT
  after the last checkpoint, it discards them.

Layout (binary, little-endian):

    [magic : 4 bytes "TWL\x00"]
    [lsn   : u64]
    [txn   : u64]
    [type  : u8]   (BEGIN, COMMIT, ABORT, PAGE, CHECKPOINT)
    [page  : u32]  (only for PAGE records; 0 otherwise)
    [len   : u32]  (length of payload)
    [payload : bytes]
    [crc32 : u32]  (over everything before the CRC)

The WAL is append-only; a successful fsync of a COMMIT record means the
transaction is durable.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import IO, Optional

from tinydb.errors import StorageError
from tinydb.storage.page import PAGE_SIZE

logger = logging.getLogger(__name__)


WAL_MAGIC: bytes = b"TWL\x00"
WAL_HEADER_SIZE: int = 4 + 8 + 8 + 1 + 4 + 4  # 29 bytes (without CRC)
WAL_CRC_SIZE: int = 4
WAL_RECORD_OVERHEAD: int = WAL_HEADER_SIZE + WAL_CRC_SIZE  # 33 bytes


class WalRecordType(IntEnum):
    BEGIN = 1
    COMMIT = 2
    ABORT = 3
    PAGE = 4
    CHECKPOINT = 5
    HEADER = 6


@dataclass
class WalRecord:
    lsn: int
    txn: int
    type: int  # WalRecordType
    page: int
    payload: bytes

    def pack(self) -> bytes:
        crc_input = (
            WAL_MAGIC
            + struct.pack(
                "<QQBI I",
                self.lsn,
                self.txn,
                int(self.type),
                self.page,
                len(self.payload),
            )
            + self.payload
        )
        crc = _crc32(crc_input)
        return crc_input + struct.pack("<I", crc)

    @classmethod
    def unpack(cls, data: bytes, offset: int = 0) -> tuple["WalRecord", int]:
        if len(data) - offset < WAL_HEADER_SIZE:
            raise ValueError("WAL record header too short")
        magic = data[offset : offset + 4]
        if magic != WAL_MAGIC:
            raise ValueError(f"bad WAL magic: {magic!r}")
        offset += 4
        lsn, txn, type_, page, length = struct.unpack_from(
            "<QQBII", data, offset
        )
        offset += 8 + 8 + 1 + 4 + 4
        if len(data) - offset < length + WAL_CRC_SIZE:
            raise ValueError("WAL record payload truncated")
        payload = data[offset : offset + length]
        offset += length
        (crc,) = struct.unpack_from("<I", data, offset)
        offset += WAL_CRC_SIZE
        # Verify CRC.
        crc_input = data[offset - WAL_RECORD_OVERHEAD - length : offset - WAL_CRC_SIZE]
        if _crc32(crc_input) != crc:
            raise ValueError(f"WAL CRC mismatch at lsn={lsn}")
        return cls(lsn=lsn, txn=txn, type=type_, page=page, payload=payload), offset


def _crc32(data: bytes) -> int:
    """CRC32 of ``data``; uses zlib for portability."""
    import zlib

    return zlib.crc32(data) & 0xFFFFFFFF


class WalWriter:
    """Append-only WAL writer with fsync on demand.

    Holds a single open file handle. Records are framed with a small
    header and a CRC so partial writes can be detected.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh: Optional[IO[bytes]] = None
        self._next_lsn: int = 0

    def open(self) -> None:
        if self._fh is not None:
            return
        if self.path.exists():
            self._fh = self.path.open("r+b")
            self._fh.seek(0, 2)  # append
            # Determine next LSN by scanning existing records.
            self._next_lsn = self._scan_max_lsn()
        else:
            self._fh = self.path.open("w+b")
            self._next_lsn = 0

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
            except Exception:
                logger.warning("WAL flush failed on close", exc_info=True)
            self._fh.close()
            self._fh = None

    def _scan_max_lsn(self) -> int:
        """Walk the WAL file to find the highest LSN written so far."""
        if self._fh is None:
            return 0
        self._fh.seek(0)
        data = self._fh.read()
        max_lsn = 0
        offset = 0
        while offset < len(data):
            try:
                rec, offset = WalRecord.unpack(data, offset)
            except ValueError:
                break
            if rec.lsn > max_lsn:
                max_lsn = rec.lsn
        return max_lsn + 1

    # ---- writers ---------------------------------------------------------

    def begin(self, txn_id: int) -> None:
        rec = WalRecord(
            lsn=self._alloc_lsn(),
            txn=txn_id,
            type=WalRecordType.BEGIN,
            page=0,
            payload=b"",
        )
        self._write(rec)

    def write_page(self, txn_id: int, page_id: int, page_bytes: bytes) -> None:
        if len(page_bytes) != PAGE_SIZE:
            raise StorageError(
                f"page payload must be {PAGE_SIZE} bytes, got {len(page_bytes)}"
            )
        rec = WalRecord(
            lsn=self._alloc_lsn(),
            txn=txn_id,
            type=WalRecordType.PAGE,
            page=page_id,
            payload=page_bytes,
        )
        self._write(rec)

    def commit(self, txn_id: int) -> None:
        rec = WalRecord(
            lsn=self._alloc_lsn(),
            txn=txn_id,
            type=WalRecordType.COMMIT,
            page=0,
            payload=b"",
        )
        self._write(rec)
        self._fsync()

    def abort(self, txn_id: int) -> None:
        rec = WalRecord(
            lsn=self._alloc_lsn(),
            txn=txn_id,
            type=WalRecordType.ABORT,
            page=0,
            payload=b"",
        )
        self._write(rec)
        self._fsync()

    def checkpoint(self) -> None:
        rec = WalRecord(
            lsn=self._alloc_lsn(),
            txn=0,
            type=WalRecordType.CHECKPOINT,
            page=0,
            payload=b"",
        )
        self._write(rec)
        self._fsync()

    def write_header(self, txn_id: int, header_bytes: bytes) -> None:
        """Record a full FileHeader snapshot under ``txn_id``.

        Recovery replays the last HEADER record of every committed
        transaction so catalog metadata (catalog_root_page,
        free_list_head, page_count) survives a crash.
        """
        if len(header_bytes) != PAGE_SIZE:
            raise StorageError(
                f"header payload must be {PAGE_SIZE} bytes, got {len(header_bytes)}"
            )
        rec = WalRecord(
            lsn=self._alloc_lsn(),
            txn=txn_id,
            type=WalRecordType.HEADER,
            page=0,
            payload=header_bytes,
        )
        self._write(rec)

    # ---- internals -------------------------------------------------------

    def _alloc_lsn(self) -> int:
        lsn = self._next_lsn
        self._next_lsn += 1
        return lsn

    def _write(self, rec: WalRecord) -> None:
        if self._fh is None:
            raise StorageError("WAL not open")
        self._fh.write(rec.pack())
        self._fh.flush()

    def _fsync(self) -> None:
        import os

        if self._fh is None:
            return
        self._fh.flush()
        os.fsync(self._fh.fileno())

    # ---- iteration (for recovery) ----------------------------------------

    def iter_records(self):
        """Yield every record in the WAL file (or empty if not open)."""
        if self._fh is None:
            return
        self._fh.seek(0)
        data = self._fh.read()
        offset = 0
        while offset < len(data):
            try:
                rec, offset = WalRecord.unpack(data, offset)
            except ValueError:
                return
            yield rec

    def truncate(self) -> None:
        """Truncate the WAL (called after a successful checkpoint)."""
        if self._fh is None:
            return
        self._fh.seek(0)
        self._fh.truncate()
        self._next_lsn = 0
