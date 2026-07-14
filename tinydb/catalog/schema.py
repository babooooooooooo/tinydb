"""Catalog schema dataclasses: tables, columns, indexes."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum

from tinydb.types import Tag


class Constraint(IntEnum):
    """Column-level constraint flags (bitmask-friendly)."""

    NONE = 0
    NOT_NULL = 1
    PRIMARY_KEY = 2
    UNIQUE = 4


@dataclass(frozen=True)
class ColumnMeta:
    name: str
    type: Tag
    constraints: int = 0  # bitmask of Constraint
    params: tuple[int, ...] = ()  # e.g. (50,) for VARCHAR(50); () for INT

    # ---- predicate helpers -----------------------------------------------

    @property
    def is_primary_key(self) -> bool:
        return bool(self.constraints & Constraint.PRIMARY_KEY)

    @property
    def is_not_null(self) -> bool:
        return bool(self.constraints & Constraint.NOT_NULL)

    @property
    def is_unique(self) -> bool:
        return bool(self.constraints & (Constraint.UNIQUE | Constraint.PRIMARY_KEY))

    # ---- serialization ---------------------------------------------------

    def pack(self) -> bytes:
        # Layout: <HIB B I* `name`
        #   u16 name_len, u32 type_val, u8 constraints, u8 param_count,
        #   param_count * u32 param values, then utf8 name.
        name_b = self.name.encode("utf-8")
        head = struct.pack(
            "<HIBB",
            len(name_b),
            int(self.type),
            self.constraints,
            len(self.params),
        )
        params_bytes = b"".join(struct.pack("<I", p) for p in self.params)
        return head + params_bytes + name_b

    @classmethod
    def unpack(cls, data: bytes, offset: int = 0) -> tuple["ColumnMeta", int]:
        (name_len, type_val, constraints, param_count) = struct.unpack_from(
            "<HIBB", data, offset
        )
        offset += 2 + 4 + 1 + 1  # 8 bytes
        params: tuple[int, ...] = ()
        if param_count:
            params = tuple(
                struct.unpack_from("<I", data, offset + 4 * i)[0]
                for i in range(param_count)
            )
            offset += 4 * param_count
        name = data[offset : offset + name_len].decode("utf-8")
        offset += name_len
        return cls(
            name=name,
            type=Tag(type_val),
            constraints=constraints,
            params=params,
        ), offset


@dataclass(frozen=True)
class IndexMeta:
    name: str
    column: str
    is_unique: bool
    root_page: int  # B+ tree root page id (0 = not yet built)

    def pack(self) -> bytes:
        name_b = self.name.encode("utf-8")
        col_b = self.column.encode("utf-8")
        return (
            struct.pack(
                "<HIBII",
                len(name_b),
                len(col_b),
                1 if self.is_unique else 0,
                self.root_page,
                0,  # reserved
            )
            + name_b
            + col_b
        )

    @classmethod
    def unpack(cls, data: bytes, offset: int = 0) -> tuple["IndexMeta", int]:
        (name_len, col_len, is_unique, root_page, _) = struct.unpack_from(
            "<HIBII", data, offset
        )
        # H(2) + I(4) + B(1) + I(4) + I(4) = 15 bytes
        offset += 15
        name = data[offset : offset + name_len].decode("utf-8")
        offset += name_len
        column = data[offset : offset + col_len].decode("utf-8")
        offset += col_len
        return cls(name=name, column=column, is_unique=bool(is_unique), root_page=root_page), offset


@dataclass(frozen=True)
class TableMeta:
    name: str
    columns: tuple[ColumnMeta, ...]
    heap_first_page: int = 0  # first heap page; 0 = empty
    heap_last_page: int = 0   # last heap page (for append)
    indexes: tuple[IndexMeta, ...] = field(default_factory=tuple)
    row_count: int = 0  # tracked for fast COUNT(*)

    def column(self, name: str) -> ColumnMeta | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None

    def primary_key(self) -> ColumnMeta | None:
        for c in self.columns:
            if c.is_primary_key:
                return c
        return None

    def index_for(self, column: str) -> IndexMeta | None:
        for idx in self.indexes:
            if idx.column == column:
                return idx
        return None

    # ---- serialization ---------------------------------------------------
    # Format per table: [name_len:u16][name bytes][num_columns:u16]
    #                   [heap_first:u32][heap_last:u32][row_count:u32]
    #                   [num_indexes:u16][reserved:u16]
    #                   columns... then indexes...
    # Then a trailing [end_marker:u8 = 0xFF] to delimit tables.

    END_MARKER = 0xFF

    def pack(self) -> bytes:
        name_b = self.name.encode("utf-8")
        header = struct.pack(
            "<HI",
            len(name_b),
            len(self.columns),
        ) + name_b + struct.pack(
            "<IIIHH",
            self.heap_first_page,
            self.heap_last_page,
            self.row_count,
            len(self.indexes),
            0,  # reserved
        )
        cols = b"".join(c.pack() for c in self.columns)
        idxs = b"".join(i.pack() for i in self.indexes)
        return header + cols + idxs + struct.pack("<B", self.END_MARKER)

    @classmethod
    def unpack(cls, data: bytes, offset: int = 0) -> tuple["TableMeta", int]:
        (name_len, num_cols) = struct.unpack_from("<HI", data, offset)
        offset += 6
        name = data[offset : offset + name_len].decode("utf-8")
        offset += name_len
        (heap_first, heap_last, row_count, num_idx, _) = struct.unpack_from(
            "<IIIHH", data, offset
        )
        # I(4) + I(4) + I(4) + H(2) + H(2) = 16 bytes
        offset += 16
        cols: list[ColumnMeta] = []
        for _ in range(num_cols):
            c, offset = ColumnMeta.unpack(data, offset)
            cols.append(c)
        idxs: list[IndexMeta] = []
        for _ in range(num_idx):
            i, offset = IndexMeta.unpack(data, offset)
            idxs.append(i)
        # Consume end marker.
        (marker,) = struct.unpack_from("<B", data, offset)
        offset += 1
        if marker != cls.END_MARKER:
            raise ValueError(f"missing table end marker at offset {offset - 1}")
        return cls(
            name=name,
            columns=tuple(cols),
            heap_first_page=heap_first,
            heap_last_page=heap_last,
            indexes=tuple(idxs),
            row_count=row_count,
        ), offset