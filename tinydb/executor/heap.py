"""Heap-page encoding for table rows.

Each HEAP page holds rows in append order. A row is encoded as:

    [row_length : u16] [num_values : u8] [value_0] [value_1] ... [value_{n-1}]

The leading ``row_length`` covers everything from itself (inclusive) to the
end of the row, so a scan can skip to the next row without deserialising.

The high bit (0x8000) of ``row_length`` marks the row as DELETED; the row
still occupies its bytes until the page is compacted.
"""

from __future__ import annotations

import struct

from tinydb.storage.page import PAGE_HEADER_SIZE
from tinydb.types import Value
from tinydb.types.serialize import deserialize, serialize, size_on_disk


_ROW_DELETED_FLAG = 0x8000
_ROW_LEN_MASK = 0x7FFF


def _row_length_bytes(value_count: int) -> int:
    """Number of bytes consumed by the row header (length + count)."""
    return 2 + 1


def _row_payload_size(values: list[Value]) -> int:
    return sum(size_on_disk(v) for v in values)


def encode_row(values: list[Value]) -> bytes:
    """Encode a list of Values as a single heap row.

    Raises ``OverflowError`` if the row would exceed the maximum length that
    fits in the u16 length field (32 KiB minus 1).
    """
    body = bytearray()
    body.append(len(values))
    for v in values:
        body += serialize(v)
    total = 2 + len(body)
    if total > _ROW_LEN_MASK:
        raise OverflowError(f"row too large to encode: {total} bytes")
    return struct.pack("<H", total) + bytes(body)


def decode_row(data: bytes, offset: int) -> tuple[list[Value], int, bool]:
    """Decode a row at ``offset`` in ``data``.

    Returns ``(values, next_offset, is_deleted)``. The next_offset is
    suitable for reading the row that immediately follows.
    """
    (raw_len,) = struct.unpack_from("<H", data, offset)
    is_deleted = bool(raw_len & _ROW_DELETED_FLAG)
    row_len = raw_len & _ROW_LEN_MASK
    (num,) = struct.unpack_from("<B", data, offset + 2)
    cur = offset + 3
    values: list[Value] = []
    for _ in range(num):
        v, cur = deserialize(data, cur)
        values.append(v)
    return values, offset + row_len, is_deleted


def mark_deleted(data: bytearray, offset: int) -> None:
    """Set the deleted flag on the row at ``offset``."""
    (raw_len,) = struct.unpack_from("<H", data, offset)
    if raw_len & _ROW_DELETED_FLAG:
        return  # already deleted
    struct.pack_into("<H", data, offset, raw_len | _ROW_DELETED_FLAG)


def page_free_space(page) -> int:
    """Bytes available for new rows in ``page``."""
    from tinydb.storage.page import PAGE_SIZE

    return PAGE_SIZE - page.free_offset


def row_fits(values: list[Value], page) -> bool:
    """True iff appending ``values`` to ``page`` would not overflow it."""
    return _row_length_bytes(0) + _row_payload_size(values) <= page_free_space(page)


def page_first_row_offset() -> int:
    """Offset (within a page's data) of the first row's length prefix."""
    return PAGE_HEADER_SIZE


def iter_rows(page):
    """Yield (row_index, values, is_deleted) for each row in ``page``."""
    data = bytes(page.data)
    offset = page_first_row_offset()
    i = 0
    while offset < page.free_offset:
        values, offset, deleted = decode_row(data, offset)
        yield i, values, deleted
        i += 1