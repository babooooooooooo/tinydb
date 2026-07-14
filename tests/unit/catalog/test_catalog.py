"""Tests for Catalog: create/drop table, persistence across reopen."""

from __future__ import annotations

from pathlib import Path

import pytest

from tinydb.catalog.catalog import Catalog
from tinydb.catalog.schema import ColumnMeta, Constraint, IndexMeta
from tinydb.errors import ConstraintError, StorageError
from tinydb.storage.disk import DiskManager
from tinydb.storage.freelist import FreeList
from tinydb.types import Tag


@pytest.fixture
def env(tmp_path: Path):
    dm = DiskManager(tmp_path / "cat.db")
    dm.open()
    fl = FreeList(dm)
    cat = Catalog(dm, fl)
    yield dm, fl, cat
    dm.close()


class TestCreateTable:
    def test_create_simple(self, env):
        _, _, cat = env
        meta = cat.create_table(
            "users",
            [
                ColumnMeta("id", Tag.INT, Constraint.PRIMARY_KEY),
                ColumnMeta("name", Tag.TEXT),
            ],
        )
        assert meta.name == "users"
        assert cat.has_table("users")

    def test_create_duplicate_raises(self, env):
        _, _, cat = env
        cat.create_table("t", [ColumnMeta("x", Tag.INT)])
        with pytest.raises(ConstraintError):
            cat.create_table("t", [ColumnMeta("y", Tag.INT)])

    def test_duplicate_column_raises(self, env):
        _, _, cat = env
        with pytest.raises(ConstraintError):
            cat.create_table(
                "t",
                [ColumnMeta("x", Tag.INT), ColumnMeta("x", Tag.TEXT)],
            )

    def test_multiple_primary_keys_raise(self, env):
        _, _, cat = env
        with pytest.raises(ConstraintError):
            cat.create_table(
                "t",
                [
                    ColumnMeta("a", Tag.INT, Constraint.PRIMARY_KEY),
                    ColumnMeta("b", Tag.INT, Constraint.PRIMARY_KEY),
                ],
            )


class TestQueries:
    def test_get_table_missing_raises(self, env):
        _, _, cat = env
        with pytest.raises(StorageError):
            cat.get_table("nope")

    def test_list_tables_sorted(self, env):
        _, _, cat = env
        cat.create_table("b", [ColumnMeta("x", Tag.INT)])
        cat.create_table("a", [ColumnMeta("x", Tag.INT)])
        cat.create_table("c", [ColumnMeta("x", Tag.INT)])
        assert cat.list_tables() == ["a", "b", "c"]


class TestDropTable:
    def test_drop_existing(self, env):
        _, _, cat = env
        cat.create_table("t", [ColumnMeta("x", Tag.INT)])
        cat.drop_table("t")
        assert not cat.has_table("t")

    def test_drop_missing_raises(self, env):
        _, _, cat = env
        with pytest.raises(StorageError):
            cat.drop_table("nope")

    def test_drop_atomic_when_heap_walk_fails(self, env):
        """If reading a heap page raises mid-drop, the table metadata must
        NOT be removed. Otherwise the catalog forgets the table while its
        heap pages stay allocated forever (a leak with no way to reclaim).
        """
        import dataclasses

        dm, _, cat = env
        cat.create_table("t", [ColumnMeta("x", Tag.INT)])
        # Give the table a non-empty heap chain so drop walks pages.
        meta = cat.get_table("t")
        cat.update_table(dataclasses.replace(meta, heap_first_page=42))

        def boom(_page_id):
            raise StorageError("simulated I/O failure")

        dm.read_page = boom
        with pytest.raises(StorageError):
            cat.drop_table("t")
        # Metadata must survive so the heap pages remain reachable.
        assert cat.has_table("t")


class TestPersistence:
    def test_save_then_reload(self, tmp_path: Path):
        # Session 1: create and save.
        dm = DiskManager(tmp_path / "persist.db")
        dm.open()
        fl = FreeList(dm)
        cat = Catalog(dm, fl)
        cat.load()
        cat.create_table(
            "users",
            [
                ColumnMeta("id", Tag.INT, Constraint.PRIMARY_KEY),
                ColumnMeta("name", Tag.TEXT),
            ],
        )
        cat.create_table("orders", [ColumnMeta("oid", Tag.INT)])
        cat.save()
        dm.close()

        # Session 2: reload.
        dm2 = DiskManager(tmp_path / "persist.db")
        dm2.open()
        fl2 = FreeList(dm2)
        cat2 = Catalog(dm2, fl2)
        cat2.load()
        assert cat2.has_table("users")
        assert cat2.has_table("orders")
        u = cat2.get_table("users")
        assert u.column("id").is_primary_key
        assert u.column("name").type is Tag.TEXT
        dm2.close()

    def test_save_with_no_tables_clears_catalog(self, tmp_path: Path):
        dm = DiskManager(tmp_path / "empty.db")
        dm.open()
        fl = FreeList(dm)
        cat = Catalog(dm, fl)
        cat.load()
        cat.create_table("t", [ColumnMeta("x", Tag.INT)])
        cat.save()
        cat.drop_table("t")
        cat.save()
        dm.close()

        dm2 = DiskManager(tmp_path / "empty.db")
        dm2.open()
        fl2 = FreeList(dm2)
        cat2 = Catalog(dm2, fl2)
        cat2.load()
        assert cat2.list_tables() == []
        dm2.close()


class TestIndexes:
    def test_add_and_drop_index(self, env):
        _, _, cat = env
        cat.create_table(
            "t",
            [
                ColumnMeta("id", Tag.INT, Constraint.PRIMARY_KEY),
                ColumnMeta("email", Tag.TEXT, Constraint.UNIQUE),
            ],
        )
        cat.add_index("t", IndexMeta("idx_email", "email", True, root_page=5))
        meta = cat.get_table("t")
        assert any(i.name == "idx_email" for i in meta.indexes)
        cat.drop_index("t", "idx_email")
        meta = cat.get_table("t")
        assert not any(i.name == "idx_email" for i in meta.indexes)

    def test_add_duplicate_index_raises(self, env):
        _, _, cat = env
        cat.create_table("t", [ColumnMeta("x", Tag.INT)])
        cat.add_index("t", IndexMeta("i", "x", False, 0))
        with pytest.raises(ConstraintError):
            cat.add_index("t", IndexMeta("i", "x", False, 0))


class TestMultiPageCatalog:
    """Regression test for the catalog-page-chain bug.

    Forces the catalog payload to span more than one 4 KiB catalog page so
    the on-disk chain linkage is exercised. Before the fix, save() wrote
    next=0 on every page except the last, so load() truncated to the first
    page and failed parsing.
    """

    def test_round_trip_spans_multiple_pages(self, tmp_path: Path):
        dm = DiskManager(tmp_path / "big.db")
        dm.open()
        fl = FreeList(dm)
        cat = Catalog(dm, fl)
        cat.load()

        # Each TableMeta packs to ~80 bytes with three INT columns; 100 of
        # them is ~8 KB, safely > one catalog page (4083 bytes payload).
        n = 100
        names = [f"t_{i:04d}_with_a_longish_name" for i in range(n)]
        for name in names:
            cat.create_table(
                name,
                [
                    ColumnMeta("a_long_column_name", Tag.INT),
                    ColumnMeta("another_long_col", Tag.TEXT),
                    ColumnMeta("third_col_name", Tag.INT),
                ],
            )
        cat.save()

        # Inspect the chain on disk: every page except the last must point
        # forward, and the chain length must be > 1.
        header = dm.read_header()
        assert header.catalog_root_page != 0
        pid = header.catalog_root_page
        chain_len = 0
        seen: set[int] = set()
        last_next_zero = False
        while pid != 0:
            assert pid not in seen, "catalog chain loops"
            seen.add(pid)
            page = dm.read_page(pid)
            chain_len += 1
            last_next_zero = page.next == 0
            pid = page.next
        assert chain_len > 1, "test should exercise multi-page catalog"
        assert last_next_zero, "final catalog page should have next=0"

        dm.close()

        # Reload in a fresh session and assert every table came back.
        dm2 = DiskManager(tmp_path / "big.db")
        dm2.open()
        cat2 = Catalog(dm2, FreeList(dm2))
        cat2.load()
        assert cat2.list_tables() == sorted(names)
        dm2.close()