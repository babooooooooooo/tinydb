"""Tests for the Write-Ahead Log (WAL) on-disk format and writer."""

from __future__ import annotations

from pathlib import Path

import pytest

from tinydb.storage.page import PAGE_SIZE
from tinydb.txn.wal import (
    WAL_HEADER_SIZE,
    WAL_MAGIC,
    WalRecord,
    WalRecordType,
    WalWriter,
)


def _fresh(tmp_path: Path) -> WalWriter:
    w = WalWriter(tmp_path / "test.wal")
    w.open()
    return w


class TestWalRecordFormat:
    def test_pack_then_unpack_round_trip(self):
        rec = WalRecord(lsn=42, txn=7, type=WalRecordType.PAGE, page=3, payload=b"hi")
        out = WalRecord.unpack(rec.pack(), 0)[0]
        assert out == rec

    def test_pack_includes_magic(self):
        rec = WalRecord(lsn=0, txn=0, type=WalRecordType.BEGIN, page=0, payload=b"")
        assert rec.pack()[:4] == WAL_MAGIC

    def test_unpack_rejects_bad_magic(self):
        bogus = b"NOPE" + b"\x00" * (WAL_HEADER_SIZE - 4)
        with pytest.raises(ValueError):
            WalRecord.unpack(bogus, 0)

    def test_unpack_rejects_truncated(self):
        with pytest.raises(ValueError):
            WalRecord.unpack(b"\x00" * 8, 0)

    def test_crc_mismatch_raises(self):
        rec = WalRecord(lsn=1, txn=1, type=WalRecordType.BEGIN, page=0, payload=b"")
        raw = bytearray(rec.pack())
        # Flip one byte in the payload area to corrupt CRC.
        raw[-5] ^= 0xFF
        with pytest.raises(ValueError):
            WalRecord.unpack(bytes(raw), 0)


class TestWalWriter:
    def test_lsn_starts_at_zero(self, tmp_path):
        w = _fresh(tmp_path)
        assert w._next_lsn == 0

    def test_begin_allocates_lsn(self, tmp_path):
        w = _fresh(tmp_path)
        w.begin(1)
        assert w._next_lsn == 1

    def test_commit_and_abort_write_records(self, tmp_path):
        w = _fresh(tmp_path)
        w.begin(1)
        w.commit(1)
        w.begin(2)
        w.abort(2)
        records = list(w.iter_records())
        assert [r.type for r in records] == [
            WalRecordType.BEGIN,
            WalRecordType.COMMIT,
            WalRecordType.BEGIN,
            WalRecordType.ABORT,
        ]

    def test_write_page_rejects_wrong_size(self, tmp_path):
        w = _fresh(tmp_path)
        w.begin(1)
        with pytest.raises(Exception):
            w.write_page(1, 5, b"too short")

    def test_write_page_persists_payload(self, tmp_path):
        w = _fresh(tmp_path)
        w.begin(1)
        payload = bytes(PAGE_SIZE)
        payload = bytearray(payload)
        payload[3:5] = b"\x64\x00"  # free_offset=100
        w.write_page(1, 7, bytes(payload))
        w.commit(1)
        records = [r for r in w.iter_records() if r.type == WalRecordType.PAGE]
        assert records[0].page == 7
        assert records[0].payload[3:5] == b"\x64\x00"

    def test_pending_pages_tracks_writes(self, tmp_path):
        # pending_pages was removed; the WAL itself is the source of truth.
        # Verify writes appear in iter_records after commit.
        w = _fresh(tmp_path)
        w.begin(1)
        w.write_page(1, 1, bytes(PAGE_SIZE))
        w.write_page(1, 2, bytes(PAGE_SIZE))
        w.commit(1)
        page_records = [
            r for r in w.iter_records() if r.type == WalRecordType.PAGE
        ]
        assert [r.page for r in page_records] == [1, 2]

    def test_reopen_resumes_lsn(self, tmp_path):
        path = tmp_path / "res.wal"
        w1 = WalWriter(path)
        w1.open()
        w1.begin(1)
        w1.write_page(1, 1, bytes(PAGE_SIZE))
        w1.commit(1)
        w1.close()
        w2 = WalWriter(path)
        w2.open()
        # Next LSN should be after the highest one written.
        w2.begin(2)
        assert w2._next_lsn == 4  # BEGIN, PAGE, COMMIT, BEGIN
        w2.close()

    def test_truncate_clears_file(self, tmp_path):
        w = _fresh(tmp_path)
        w.begin(1)
        w.commit(1)
        w.truncate()
        assert list(w.iter_records()) == []
        assert w._next_lsn == 0

    def test_checkpoint_record_written(self, tmp_path):
        w = _fresh(tmp_path)
        w.checkpoint()
        records = list(w.iter_records())
        assert len(records) == 1
        assert records[0].type == WalRecordType.CHECKPOINT
        assert records[0].txn == 0

    def test_close_idempotent(self, tmp_path):
        w = _fresh(tmp_path)
        w.close()
        w.close()  # should not raise