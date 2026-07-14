"""Tests for ColumnMeta, IndexMeta, TableMeta serialization."""

from __future__ import annotations

import pytest

from tinydb.catalog.schema import ColumnMeta, Constraint, IndexMeta, TableMeta
from tinydb.types import Tag


class TestColumnMeta:
    def test_pack_unround_trip(self):
        c = ColumnMeta("age", Tag.INT, Constraint.NOT_NULL)
        out, offset = ColumnMeta.unpack(c.pack())
        assert out == c
        assert offset == len(c.pack())

    def test_default_params_empty_tuple(self):
        """ColumnMeta constructed without params must default to ()."""
        c = ColumnMeta("age", Tag.INT)
        assert c.params == ()

    def test_pack_unpack_with_varchar_params(self):
        c = ColumnMeta("name", Tag.VARCHAR, 0, params=(50,))
        out, offset = ColumnMeta.unpack(c.pack())
        assert out == c
        assert out.params == (50,)

    def test_pack_unpack_with_char_params(self):
        c = ColumnMeta("code", Tag.CHAR, 0, params=(4,))
        out, offset = ColumnMeta.unpack(c.pack())
        assert out == c
        assert out.params == (4,)

    def test_pack_unpack_with_decimal_params(self):
        c = ColumnMeta("price", Tag.DECIMAL, 0, params=(10, 2))
        out, offset = ColumnMeta.unpack(c.pack())
        assert out == c
        assert out.params == (10, 2)

    def test_pack_unpack_params_with_constraints(self):
        c = ColumnMeta("name", Tag.VARCHAR, Constraint.NOT_NULL, params=(50,))
        out, offset = ColumnMeta.unpack(c.pack())
        assert out == c
        assert out.is_not_null
        assert out.params == (50,)

    def test_constraint_flags(self):
        c = ColumnMeta("id", Tag.INT, Constraint.PRIMARY_KEY)
        assert c.is_primary_key
        assert c.is_unique  # PK implies unique
        assert c.is_not_null is False  # PK does NOT imply NOT NULL by default

    def test_primary_key_implies_unique(self):
        c = ColumnMeta("id", Tag.INT, Constraint.PRIMARY_KEY)
        assert c.is_unique

    def test_not_null_check(self):
        c = ColumnMeta("name", Tag.TEXT, Constraint.NOT_NULL)
        assert c.is_not_null
        assert not c.is_primary_key


class TestIndexMeta:
    def test_pack_unpack(self):
        i = IndexMeta("idx_age", "age", is_unique=False, root_page=42)
        out, offset = IndexMeta.unpack(i.pack())
        assert out == i
        assert offset == len(i.pack())

    def test_unique_flag(self):
        i = IndexMeta("u", "x", is_unique=True, root_page=0)
        assert i.is_unique


class TestTableMeta:
    def _sample(self) -> TableMeta:
        return TableMeta(
            name="users",
            columns=(
                ColumnMeta("id", Tag.INT, Constraint.PRIMARY_KEY),
                ColumnMeta("name", Tag.TEXT, Constraint.NOT_NULL),
                ColumnMeta("email", Tag.TEXT, Constraint.UNIQUE),
            ),
            indexes=(IndexMeta("idx_email", "email", True, root_page=7),),
            row_count=10,
        )

    def test_pack_unpack(self):
        t = self._sample()
        out, offset = TableMeta.unpack(t.pack())
        assert out == t
        assert offset == len(t.pack())

    def test_lookup_helpers(self):
        t = self._sample()
        assert t.column("id").is_primary_key
        assert t.primary_key().name == "id"
        assert t.index_for("email").name == "idx_email"
        assert t.index_for("missing") is None
        assert t.column("missing") is None

    def test_empty_table(self):
        t = TableMeta(name="t", columns=())
        out, _ = TableMeta.unpack(t.pack())
        assert out.name == "t"
        assert out.columns == ()