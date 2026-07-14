"""Transaction manager: BEGIN / COMMIT / ROLLBACK, single-writer enforcement.

A transaction is identified by a 64-bit id assigned by the manager on
BEGIN. The manager coordinates three things:

* **single-writer enforcement**: only one transaction may be open at a
  time. Nested BEGINs raise an error.
* **WAL logging**: every data-page mutation is recorded in the WAL
  before the in-memory page is released.
* **rollback tracking**: a per-transaction undo log of (page_id,
  pre-image) lets ROLLBACK restore the original bytes.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from typing import Optional

from tinydb.errors import TransactionError
from tinydb.storage.buffer import BufferPool
from tinydb.storage.disk import DiskManager
from tinydb.storage.page import PAGE_SIZE, FileHeader, Page
from tinydb.txn.wal import WalRecordType, WalWriter

logger = logging.getLogger(__name__)


class TransactionManager:
    """Coordinates WAL writes, undo logging, and recovery on open."""

    def __init__(self, disk: DiskManager, pool: BufferPool, wal_path: Path) -> None:
        self.disk = disk
        self.pool = pool
        self.wal_path = Path(wal_path)
        self.wal = WalWriter(self.wal_path)
        self._next_txn: int = 1
        self._current_txn: int = 0
        # Per-transaction undo log: txn_id -> list of (page_id, before_bytes).
        self._undo: dict[int, list[tuple[int, bytes]]] = {}
        # Deferred frees during the current txn; flushed on commit, dropped
        # on rollback. Lets DROP TABLE inside a txn be undone cleanly.
        self._deferred_frees: list[int] = []
        # Optional callback the Database wires up so we can snapshot/restore
        # non-page state (the catalog) on begin/rollback.
        self._snapshot: Optional[object] = None
        self._restore: Optional[object] = None
        self._in_recovery = False

    def set_state_callbacks(self, snapshot, restore) -> None:
        """Register callbacks for catalog state snapshot/restore.

        ``snapshot()`` returns a deep copy of mutable state taken at BEGIN;
        ``restore(snapshot)`` restores that state at ROLLBACK.
        """
        self._snapshot = snapshot
        self._restore = restore

    def open(self) -> None:
        self.wal.open()

    def close(self) -> None:
        if self._current_txn != 0:
            # Implicit rollback on close.
            try:
                self.rollback()
            except Exception:
                logger.warning(
                    "implicit rollback failed during transaction manager "
                    "close; database may be left in an uncommitted state",
                    exc_info=True,
                )
        self.wal.close()

    # ---- transaction lifecycle -------------------------------------------

    @property
    def in_transaction(self) -> bool:
        return self._current_txn != 0

    def begin(self) -> int:
        if self._current_txn != 0:
            raise TransactionError("a transaction is already in progress")
        tid = self._next_txn
        self._next_txn += 1
        self._current_txn = tid
        self._undo[tid] = []
        self._deferred_frees = []
        if self._snapshot is not None:
            self._undo[f"{tid}:snapshot"] = self._snapshot()
        self.wal.begin(tid)
        return tid

    def commit(self) -> None:
        if self._current_txn == 0:
            raise TransactionError("no active transaction")
        tid = self._current_txn
        # Apply deferred frees BEFORE writing the COMMIT record so the
        # resulting freelist header change is part of this txn's WAL
        # trail — recovery replays HEADER records for committed txns.
        if self._deferred_frees:
            from tinydb.storage.freelist import FreeList  # local import to avoid cycle

            fl = FreeList(self.disk, self.pool)
            for pid in self._deferred_frees:
                try:
                    fl.free(pid)
                except Exception:
                    logger.warning(
                        "deferred free of page %d failed during commit of "
                        "txn %d; page leaked", pid, tid, exc_info=True
                    )
            # Re-snapshot the header so the WAL sees the latest value.
            self.wal.write_header(tid, self.disk.read_header().pack())
        # Force dirty pages to disk so the WAL sees their post-images.
        # Without this, a crash between commit and pool eviction loses
        # the changes (recovery would replay an empty page set).
        self.pool.flush_all()
        self.wal.commit(tid)
        self._deferred_frees = []
        self._undo.pop(tid, None)
        self._current_txn = 0

    def rollback(self) -> None:
        if self._current_txn == 0:
            raise TransactionError("no active transaction")
        tid = self._current_txn
        # Drop deferred frees — the pages they refer to were never
        # actually freed on disk, so nothing to undo.
        self._deferred_frees = []
        # Restore non-page state (catalog snapshot) first.
        snap_key = f"{tid}:snapshot"
        if snap_key in self._undo and self._restore is not None:
            self._restore(self._undo.pop(snap_key))
        # Restore the pre-image of every page we touched.
        for page_id, before_bytes in reversed(self._undo.get(tid, [])):
            page = self.pool.fetch_page(page_id)
            try:
                # Restore both the raw bytes AND the in-page header fields.
                page.data[:] = before_bytes
                # Re-read the header from the restored bytes so the
                # in-memory private attrs are coherent. The property
                # setters are not used here because we want to avoid
                # re-triggering _write_header on top of data we just
                # restored wholesale.
                page._read_header()
                page.dirty = True  # mark for write-through on next flush
                # Force the restored pre-image to disk NOW (bypass WAL).
                # Without this the on-disk bytes still hold the post-image
                # if anything flushed mid-transaction; a crash before the
                # next eviction would then commit the aborted write.
                self.disk.write_page(page)
                page.dirty = False  # it is now on disk
            finally:
                self.pool.unpin_page(page_id, dirty=False)
        self.wal.abort(tid)
        self._undo.pop(tid, None)
        self._current_txn = 0

    def defer_free(self, page_id: int) -> None:
        """Register a page to be freed on COMMIT; ignored on ROLLBACK.

        Called by FreeList when inside a transaction. Outside a
        transaction the caller must invoke ``FreeList.free`` directly.
        """
        if self._current_txn == 0:
            raise TransactionError("defer_free requires an active transaction")
        self._deferred_frees.append(page_id)

    # ---- page-mutation logging -------------------------------------------

    def log_page_write(self, page: Page) -> None:
        """Record a page mutation for the current transaction.

        If no transaction is active, the call is a no-op (auto-commit
        mode). If a transaction is active, we snapshot the page's bytes
        BEFORE the caller mutates it (this must be called BEFORE the
        mutation); the caller's subsequent mutation produces the
        post-image which will be written to the WAL on commit.
        """
        if self._current_txn == 0:
            return
        # Capture the pre-image (current bytes) so rollback can restore.
        before = bytes(page.data)
        self._undo.setdefault(self._current_txn, []).append(
            (page.page_id, before)
        )

    def flush_page(self, page: Page) -> None:
        """Write-through hook called by BufferPool before disk I/O.

        If a transaction is open, the page is first appended to the WAL
        (so recovery sees the mutation). Then the page is handed to
        DiskManager.write_page for the actual write.
        """
        if self._current_txn != 0:
            self.wal.write_page(
                self._current_txn, page.page_id, bytes(page.data)
            )
        self.disk.write_page(page)

    def flush_header(self, header: FileHeader) -> None:
        """Write-through hook for FileHeader updates.

        Logs the header to the WAL under the current transaction (if
        any) so recovery can replay catalog / freelist metadata
        changes that landed inside a committed transaction.
        """
        if self._current_txn != 0:
            self.wal.write_header(self._current_txn, header.pack())
        self.disk.write_header(header)

    # ---- recovery --------------------------------------------------------

    def recover(self) -> None:
        """Replay committed transactions and discard uncommitted ones.

        Walks the WAL in order, applying the last PAGE and HEADER
        record for each page-id / header-slot encountered under a
        committed transaction. Any transactions without a COMMIT (or
        with an explicit ABORT) are ignored. The data file is rewritten
        with the final committed state of every touched page and the
        final committed FileHeader.
        """
        self._in_recovery = True
        committed: dict[int, bool] = {}
        last_page: dict[int, bytes] = {}
        last_header: bytes | None = None
        max_txn = 0
        for rec in self.wal.iter_records():
            if rec.txn > max_txn:
                max_txn = rec.txn
            if rec.type == WalRecordType.BEGIN:
                committed[rec.txn] = False
            elif rec.type == WalRecordType.COMMIT:
                committed[rec.txn] = True
            elif rec.type == WalRecordType.ABORT:
                committed.pop(rec.txn, None)
            elif rec.type == WalRecordType.PAGE:
                if committed.get(rec.txn) is True:
                    last_page[rec.page] = rec.payload
            elif rec.type == WalRecordType.HEADER:
                if committed.get(rec.txn) is True:
                    last_header = rec.payload
        # Apply recovered data pages.
        for page_id, data in last_page.items():
            page = Page.from_bytes(page_id, data)
            self.disk.write_page(page)
        # Apply the most recent committed header (if any).
        if last_header is not None:
            header = FileHeader.unpack(last_header)
            header.validate()
            self.disk.write_header(header)
        self._next_txn = max_txn + 1
        # Truncate WAL after successful recovery.
        self.wal.truncate()
        self._in_recovery = False

    # ---- checkpoint ------------------------------------------------------

    def checkpoint(self) -> None:
        """Flush all dirty pages, record CHECKPOINT, truncate WAL."""
        if self._current_txn != 0:
            raise TransactionError("cannot checkpoint while in a transaction")
        # The caller is expected to have flushed the buffer pool before
        # calling this. The WAL CHECKPOINT record is the durability marker.
        self.wal.checkpoint()
        self.wal.truncate()
