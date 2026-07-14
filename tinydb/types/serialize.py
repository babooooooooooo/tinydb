"""Binary serialization for Value.

Format on disk (per value):
    [tag : u8] [payload...]
    INT    : 8 bytes signed little-endian (q)
    FLOAT  : 8 bytes little-endian double (d)
    BOOL   : 1 byte (0x00 / 0x01)
    TEXT   : [length : u32 LE] [utf8 bytes]
    NULL   : 0 bytes (tag alone)
    VARCHAR/CHAR/DECIMAL : same as TEXT (payload is str; length cap is in
                            ColumnMeta.params, enforced at coerce time)
    DATE/TIME/TIMESTAMP/SMALLINT/BIGINT : 8 bytes signed LE (q)

``serialize`` produces a complete ``bytes`` object; ``deserialize`` reads
a single value from a buffer at the given offset and returns the new offset.
"""

from __future__ import annotations

import struct

from tinydb.types.value import Tag, Value

_INT_FMT = struct.Struct("<bq")        # tag(1) + int64(8) = 9 bytes
_FLOAT_FMT = struct.Struct("<bd")      # tag(1) + float64(8) = 9 bytes
_BOOL_FMT = struct.Struct("<B?")       # tag(1) + bool(1) = 2 bytes
_NULL_FMT = struct.Struct("<B")        # tag(1) = 1 byte
_INT64_FMT = struct.Struct("<bq")      # tag(1) + int64(8) = 9 bytes (DATE/TIME/TIMESTAMP/SMALLINT/BIGINT)


def size_on_disk(value: Value) -> int:
    """Return the number of bytes ``serialize(value)`` will produce."""
    if value.tag is Tag.INT:
        return _INT_FMT.size
    if value.tag is Tag.FLOAT:
        return _FLOAT_FMT.size
    if value.tag is Tag.BOOL:
        return _BOOL_FMT.size
    if value.tag is Tag.NULL:
        return _NULL_FMT.size
    if value.tag in (Tag.TEXT, Tag.VARCHAR, Tag.CHAR, Tag.DECIMAL):
        return 1 + 4 + len(value.payload.encode("utf-8"))
    if value.tag in (Tag.DATE, Tag.TIME, Tag.TIMESTAMP, Tag.SMALLINT, Tag.BIGINT):
        return _INT64_FMT.size
    raise ValueError(f"unknown tag {value.tag!r}")


def serialize(value: Value) -> bytes:
    if value.tag is Tag.INT:
        return _INT_FMT.pack(Tag.INT, value.payload)
    if value.tag is Tag.FLOAT:
        return _FLOAT_FMT.pack(Tag.FLOAT, value.payload)
    if value.tag is Tag.BOOL:
        return _BOOL_FMT.pack(Tag.BOOL, value.payload)
    if value.tag is Tag.NULL:
        return _NULL_FMT.pack(Tag.NULL)
    if value.tag in (Tag.TEXT, Tag.VARCHAR, Tag.CHAR, Tag.DECIMAL):
        encoded = value.payload.encode("utf-8")
        return struct.pack("<BI", int(value.tag), len(encoded)) + encoded
    if value.tag in (Tag.DATE, Tag.TIME, Tag.TIMESTAMP, Tag.SMALLINT, Tag.BIGINT):
        return _INT64_FMT.pack(int(value.tag), value.payload)
    raise ValueError(f"unknown tag {value.tag!r}")


def deserialize(data: bytes, offset: int = 0) -> tuple[Value, int]:
    """Read one value from ``data`` starting at ``offset``.

    Returns ``(value, new_offset)``. Raises ``ValueError`` on truncation or
    an unknown tag byte.
    """
    if offset >= len(data):
        raise ValueError("buffer underrun reading tag byte")
    try:
        (tag,) = struct.unpack_from("<B", data, offset)
    except struct.error as e:
        raise ValueError(str(e)) from e
    offset += 1
    try:
        if tag == Tag.INT:
            (v,) = struct.unpack_from("<q", data, offset)
            return Value.int_(v), offset + 8
        if tag == Tag.FLOAT:
            (v,) = struct.unpack_from("<d", data, offset)
            return Value.float_(v), offset + 8
        if tag == Tag.BOOL:
            (v,) = struct.unpack_from("<?", data, offset)
            return Value.bool_(bool(v)), offset + 1
        if tag == Tag.NULL:
            return Value.null(), offset
        if tag in (Tag.TEXT, Tag.VARCHAR, Tag.CHAR, Tag.DECIMAL):
            if offset + 4 > len(data):
                raise ValueError("buffer underrun reading text length")
            (length,) = struct.unpack_from("<I", data, offset)
            offset += 4
            end = offset + length
            if end > len(data):
                raise ValueError("buffer underrun reading text payload")
            payload = data[offset:end].decode("utf-8")
            if tag == Tag.TEXT:
                return Value.text(payload), end
            if tag == Tag.VARCHAR:
                return Value.varchar(payload), end
            if tag == Tag.CHAR:
                return Value.char(payload), end
            return Value.decimal(payload), end
        if tag in (Tag.DATE, Tag.TIME, Tag.TIMESTAMP, Tag.SMALLINT, Tag.BIGINT):
            (v,) = struct.unpack_from("<q", data, offset)
            if tag == Tag.DATE:
                return Value.date(v), offset + 8
            if tag == Tag.TIME:
                return Value.time(v), offset + 8
            if tag == Tag.TIMESTAMP:
                return Value.timestamp(v), offset + 8
            if tag == Tag.SMALLINT:
                return Value.smallint(v), offset + 8
            return Value.bigint(v), offset + 8
        raise ValueError(f"unknown tag byte {tag}")
    except struct.error as e:
        raise ValueError(str(e)) from e