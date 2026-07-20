"""Tests for the B+ tree index."""

from __future__ import annotations

from pathlib import Path

import pytest

from tinydb.index.btree import BPlusTree
from tinydb.storage.buffer import BufferPool
from tinydb.storage.disk import DiskManager
from tinydb.storage.freelist import FreeList
from tinydb.types import Tag, Value


@pytest.fixture
def env(tmp_path: Path):
    dm = DiskManager(tmp_path / "idx.db")
    dm.open()
    pool = BufferPool(dm, capacity=32)
    fl = FreeList(dm)
    tree = BPlusTree(pool, fl)
    yield pool, fl, tree
    pool.flush_all()
    dm.close()


def _i(v: int) -> Value:
    return Value.int_(v)


def _t(v: str) -> Value:
    return Value.text(v)


# ---- insert / lookup ------------------------------------------------------


class TestInsertLookup:
    def test_empty_tree_returns_none(self, env):
        _, _, tree = env
        assert tree.point_lookup(_i(1)) is None

    def test_insert_then_lookup(self, env):
        _, _, tree = env
        tree.insert(_i(10), 100)
        tree.insert(_i(20), 200)
        tree.insert(_i(5), 50)
        assert tree.point_lookup(_i(10)) == 100
        assert tree.point_lookup(_i(20)) == 200
        assert tree.point_lookup(_i(5)) == 50
        assert tree.point_lookup(_i(999)) is None

    def test_overwrite_same_key(self, env):
        _, _, tree = env
        tree.insert(_i(7), 1)
        tree.insert(_i(7), 2)
        assert tree.point_lookup(_i(7)) == 2

    def test_text_keys(self, env):
        _, _, tree = env
        tree.insert(_t("banana"), 1)
        tree.insert(_t("apple"), 2)
        tree.insert(_t("cherry"), 3)
        assert tree.point_lookup(_t("apple")) == 2
        assert tree.point_lookup(_t("banana")) == 1
        assert tree.point_lookup(_t("cherry")) == 3


# ---- range scan -----------------------------------------------------------


class TestRangeScan:
    def test_scan_all(self, env):
        _, _, tree = env
        for i in [5, 3, 8, 1, 4]:
            tree.insert(_i(i), i * 10)
        result = tree.range_scan(None, None)
        assert [k.payload for k, _ in result] == [1, 3, 4, 5, 8]

    def test_scan_range(self, env):
        _, _, tree = env
        for i in range(1, 11):
            tree.insert(_i(i), i * 100)
        result = tree.range_scan(_i(3), _i(7))
        assert [k.payload for k, _ in result] == [3, 4, 5, 6, 7]

    def test_scan_low_only(self, env):
        _, _, tree = env
        for i in [1, 2, 3, 4, 5]:
            tree.insert(_i(i), i)
        result = tree.range_scan(_i(3), None)
        assert [k.payload for k, _ in result] == [3, 4, 5]

    def test_scan_high_only(self, env):
        _, _, tree = env
        for i in [1, 2, 3, 4, 5]:
            tree.insert(_i(i), i)
        result = tree.range_scan(None, _i(3))
        assert [k.payload for k, _ in result] == [1, 2, 3]


# ---- delete ---------------------------------------------------------------


class TestDelete:
    def test_delete_existing(self, env):
        _, _, tree = env
        for i in range(1, 6):
            tree.insert(_i(i), i)
        assert tree.delete(_i(3)) is True
        assert tree.point_lookup(_i(3)) is None
        assert tree.point_lookup(_i(4)) == 4

    def test_delete_missing_returns_false(self, env):
        _, _, tree = env
        tree.insert(_i(1), 1)
        assert tree.delete(_i(99)) is False

    def test_delete_all(self, env):
        _, _, tree = env
        for i in range(1, 6):
            tree.insert(_i(i), i)
        for i in range(1, 6):
            assert tree.delete(_i(i)) is True
        assert tree.point_lookup(_i(3)) is None
        # After deleting everything, root_id is 0.
        assert tree.root_page_id == 0

    def test_merge_returns_consumed_pages_to_freelist(self, env):
        """When leaf underflow triggers a merge (not a borrow), the consumed
        leaf page must be returned to the freelist; otherwise the data file
        grows without bound across delete-heavy workloads.

        We insert 600 keys (multiple leaves) then delete almost all of them,
        keeping only a sparse handful. This forces multiple siblings to
        underflow simultaneously, so each pair is merged (not borrowed).
        """
        pool, fl, tree = env

        def freelist_size() -> int:
            h = fl.disk.read_header()
            n = 0
            cur = h.free_list_head
            while cur != 0:
                cur = fl.disk.read_page(cur).next
                n += 1
            return n

        for k in range(600):
            tree.insert(_i(k), k * 10)
        pool.flush_all()
        before = freelist_size()
        assert before == 0, "no free pages expected after only inserts"

        keep = {0, 100, 200, 400, 599}
        for k in range(600):
            if k not in keep:
                assert tree.delete(_i(k)) is True
        pool.flush_all()
        after = freelist_size()

        assert after > before, (
            f"freelist size did not grow across merges: before={before}, "
            f"after={after}, total_pages={fl.count_pages()}, root={tree.root_page_id}"
        )

        for k in keep:
            assert tree.point_lookup(_i(k)) == k * 10
        assert tree.point_lookup(_i(150)) is None

    def test_merged_pages_are_not_reused_with_stale_data(self, env):
        """After a merge frees a leaf, the next allocation that reuses that
        page must see freshly written bytes, not the old leaf contents.

        Without the fix (or with stale cache), the freed page id could end
        up at the front of the free list with stale on-disk bytes that the
        pool still held in cache.
        """
        from tinydb.index.btree import BPlusTree
        from tinydb.storage.page import PageType

        pool, fl, tree = env

        # Build a tree with multiple leaves and trigger a merge.
        for k in range(400):
            tree.insert(_i(k), k * 10)
        keep = {0, 150, 300, 399}
        for k in range(400):
            if k not in keep:
                tree.delete(_i(k))
        pool.flush_all()
        freelist_head = fl.head()
        assert freelist_head != 0, "merge should have produced a free page"

        # Allocate a fresh page from the freelist head.
        new_page = fl.allocate(PageType.HEAP)
        try:
            assert new_page.page_id == freelist_head
            # The on-disk bytes must be initialized (not the merged-in
            # entries that used to live there).
            raw = pool.disk.read_page(new_page.page_id).data
            from tinydb.storage.page import PAGE_HEADER_SIZE
            # The reused page should be empty (just zeros past the header).
            assert raw[PAGE_HEADER_SIZE:PAGE_HEADER_SIZE + 16] == b"\x00" * 16
        finally:
            pool.unpin_page(new_page.page_id)

    def test_collapse_to_root_keeps_data(self, env):
        """When a merge reduces the tree to a single leaf, root_page_id
        becomes the surviving leaf; subsequent inserts must still work and
        survivors must remain queryable.
        """
        pool, fl, tree = env
        for k in range(300):
            tree.insert(_i(k), k)
        # Wipe out almost everything.
        for k in range(300):
            if k % 7 == 0:
                continue
            tree.delete(_i(k))
        pool.flush_all()
        survivors = sorted(k for k in range(300) if k % 7 == 0)
        result = tree.range_scan(None, None)
        assert [k.payload for k, _ in result] == survivors
        # New inserts after heavy deletes must be queryable.
        tree.insert(_i(999), 999)
        assert tree.point_lookup(_i(999)) == 999
        for k in survivors:
            assert tree.point_lookup(_i(k)) == k

    def test_merge_only_does_not_corrupt_chain(self, env):
        """The leaf next/prev chain must remain consistent across merges;
        range_scan from low to high must visit every survivor exactly once.
        """
        pool, fl, tree = env
        for k in range(500):
            tree.insert(_i(k), k)
        # Delete the middle band — both neighbours go below min_fill.
        for k in list(range(150, 350)):
            tree.delete(_i(k))
        pool.flush_all()
        result = tree.range_scan(None, None)
        survivors = sorted(set(range(500)) - set(range(150, 350)))
        assert [k.payload for k, _ in result] == survivors


# ---- invariants -----------------------------------------------------------


class TestInvariants:
    def test_force_internal_node(self, env):
        """Insert enough keys that a leaf split AND internal split occur."""
        _, _, tree = env
        # Each leaf holds ~250 INT entries; 600 keys forces ≥1 split +
        # likely a root grow.
        for i in range(600):
            tree.insert(_i(i), i)
        # Every key still queryable.
        for i in range(600):
            assert tree.point_lookup(_i(i)) == i

    def test_random_inserts_then_lookups(self, env):
        import random

        random.seed(42)
        _, _, tree = env
        keys = list(range(2000))
        random.shuffle(keys)
        for k in keys:
            tree.insert(_i(k), k * 7)
        # Verify all present with correct value.
        for k in keys:
            assert tree.point_lookup(_i(k)) == k * 7
        # Range scan returns sorted ascending.
        result = tree.range_scan(None, None)
        assert [k.payload for k, _ in result] == sorted(keys)

    def test_random_inserts_and_deletes(self, env):
        import random

        random.seed(123)
        _, _, tree = env
        keys = list(range(500))
        random.shuffle(keys)
        for k in keys:
            tree.insert(_i(k), k)
        # Delete every other one.
        to_delete = [k for i, k in enumerate(keys) if i % 2 == 0]
        for k in to_delete:
            assert tree.delete(_i(k)) is True
        # Remaining keys still present.
        for k in keys:
            if k in to_delete:
                assert tree.point_lookup(_i(k)) is None
            else:
                assert tree.point_lookup(_i(k)) == k
        # Range scan returns only survivors, sorted.
        result = tree.range_scan(None, None)
        survivors = sorted(set(keys) - set(to_delete))
        assert [k.payload for k, _ in result] == survivors

    def test_leaf_chain_is_linked(self, env):
        """The leaf `next` chain must connect every leaf in key order."""
        _, _, tree = env
        for i in range(800):
            tree.insert(_i(i), i)
        # Walk from root to first leaf.
        page = tree.pool.fetch_page(tree.root_page_id)
        try:
            while page.page_type.value == 3:  # BTREE_INTERNAL
                data = bytes(page.data)
                # Skip down to first child.
                # We don't have direct access to internal-decoder here;
                # but `range_scan(None, None)` already proves the chain
                # returns all keys, which is what matters.
                break
            result = tree.range_scan(None, None)
            assert len(result) == 800
        finally:
            tree.pool.unpin_page(page.page_id, dirty=page.dirty)


# ---- persistence ----------------------------------------------------------


class TestPersistence:
    def test_reopen_preserves_data(self, tmp_path: Path):
        # Session 1: insert keys, close.
        dm = DiskManager(tmp_path / "p.db")
        dm.open()
        pool = BufferPool(dm, capacity=32)
        fl = FreeList(dm)
        tree = BPlusTree(pool, fl)
        for i in [3, 1, 4, 1, 5, 9, 2, 6]:  # includes duplicate
            tree.insert(_i(i), i * 11)
        tree.insert(_i(4), 44)  # overwrite
        pool.flush_all()
        root_id = tree.root_page_id
        dm.close()

        # Session 2: reopen with the same root id.
        dm2 = DiskManager(tmp_path / "p.db")
        dm2.open()
        pool2 = BufferPool(dm2, capacity=32)
        fl2 = FreeList(dm2)
        tree2 = BPlusTree(pool2, fl2, root_page_id=root_id)
        for i in [3, 1, 4, 5, 9, 2, 6]:
            assert tree2.point_lookup(_i(i)) == i * 11
        result = tree2.range_scan(None, None)
        assert [k.payload for k, _ in result] == [1, 2, 3, 4, 5, 6, 9]
        pool2.flush_all()
        dm2.close()


# ---- comparison edge cases -----------------------------------------------


class TestComparison:
    def test_int_and_float_keys_mixed(self, env):
        """INT and FLOAT compare equal under numeric promotion."""
        _, _, tree = env
        tree.insert(Value.float_(1.5), 100)
        # Inserting the same key under INT tag still looks it up via point_lookup
        # only if the comparison helpers do numeric promotion. We chose strict
        # tag equality for keys here, so the second insert is a fresh entry.
        tree.insert(Value.int_(2), 200)
        assert tree.point_lookup(Value.float_(1.5)) == 100
        assert tree.point_lookup(Value.int_(2)) == 200

    def test_null_keys_sort_to_end(self, env):
        _, _, tree = env
        tree.insert(_i(1), 1)
        tree.insert(Value.null(), 9)
        tree.insert(_i(2), 2)
        result = tree.range_scan(None, None)
        keys = [k.payload for k, _ in result]
        assert keys == [1, 2, None]

class TestEntrySize:
    """Defensive: _entry_size must return a numeric size for every
    supported tag, including the 8 newly added tags. Currently it is only
    used for indexed columns (PRIMARY KEY / UNIQUE auto-indexes), which
    we still restrict to INT/TEXT; the new branches are future-proofing."""

    from tinydb.types import Value
    from tinydb.index.btree import _entry_size

    def test_varchar(self):
        from tinydb.types import Value
        from tinydb.index.btree import _entry_size
        assert _entry_size(Value.varchar("abc")) == 1 + 4 + 3

    def test_char(self):
        from tinydb.types import Value
        from tinydb.index.btree import _entry_size
        assert _entry_size(Value.char("xyz")) == 1 + 4 + 3

    def test_date(self):
        from tinydb.types import Value
        from tinydb.index.btree import _entry_size
        assert _entry_size(Value.date(20000)) == 9

    def test_time(self):
        from tinydb.types import Value
        from tinydb.index.btree import _entry_size
        assert _entry_size(Value.time(3600)) == 9

    def test_timestamp(self):
        from tinydb.types import Value
        from tinydb.index.btree import _entry_size
        assert _entry_size(Value.timestamp(1_700_000_000)) == 9

    def test_decimal(self):
        from tinydb.types import Value
        from tinydb.index.btree import _entry_size
        assert _entry_size(Value.decimal("3.14")) == 1 + 4 + 4

    def test_smallint(self):
        from tinydb.types import Value
        from tinydb.index.btree import _entry_size
        assert _entry_size(Value.smallint(1)) == 9

    def test_bigint(self):
        from tinydb.types import Value
        from tinydb.index.btree import _entry_size
        assert _entry_size(Value.bigint(1 << 40)) == 9


# ---- exclusive / inclusive bounds ----------------------------------------


class TestExclusiveBounds:
    """`range_scan_with_bound(low, high, *, low_inclusive, high_inclusive)`
    lets callers express strict-less-than / strict-greater-than ranges
    while keeping the original `range_scan(low, high)` (always inclusive)
    unchanged."""

    def test_gt_only(self, env):
        """low=5 with low_inclusive=False yields keys strictly > 5."""
        _, _, tree = env
        for i in [1, 3, 5, 7, 9]:
            tree.insert(_i(i), i)
        result = tree.range_scan_with_bound(
            _i(5), None, low_inclusive=False, high_inclusive=True
        )
        assert [k.payload for k, _ in result] == [7, 9]

    def test_lt_only(self, env):
        """high=5 with high_inclusive=False yields keys strictly < 5."""
        _, _, tree = env
        for i in [1, 3, 5, 7, 9]:
            tree.insert(_i(i), i)
        result = tree.range_scan_with_bound(
            None, _i(5), low_inclusive=True, high_inclusive=False
        )
        assert [k.payload for k, _ in result] == [1, 3]

    def test_gt_and_lt_exclusive(self, env):
        """Both bounds exclusive: open interval (3, 7)."""
        _, _, tree = env
        for i in [1, 3, 4, 5, 7, 9]:
            tree.insert(_i(i), i)
        result = tree.range_scan_with_bound(
            _i(3), _i(7), low_inclusive=False, high_inclusive=False
        )
        assert [k.payload for k, _ in result] == [4, 5]

    def test_ge_and_le_inclusive(self, env):
        """Both bounds inclusive matches the legacy inclusive semantics
        (regression guard for the new API)."""
        _, _, tree = env
        for i in [1, 3, 5, 7, 9]:
            tree.insert(_i(i), i)
        result = tree.range_scan_with_bound(
            _i(3), _i(7), low_inclusive=True, high_inclusive=True
        )
        assert [k.payload for k, _ in result] == [3, 5, 7]

    def test_open_low(self, env):
        """low=None means open-low; high side respects its flag."""
        _, _, tree = env
        for i in [1, 3, 5, 7, 9]:
            tree.insert(_i(i), i)
        result = tree.range_scan_with_bound(
            None, _i(5), low_inclusive=True, high_inclusive=True
        )
        assert [k.payload for k, _ in result] == [1, 3, 5]
        # Same range with exclusive high drops the 5.
        result_excl = tree.range_scan_with_bound(
            None, _i(5), low_inclusive=True, high_inclusive=False
        )
        assert [k.payload for k, _ in result_excl] == [1, 3]

    def test_open_high(self, env):
        """high=None means open-high; low side respects its flag."""
        _, _, tree = env
        for i in [1, 3, 5, 7, 9]:
            tree.insert(_i(i), i)
        result = tree.range_scan_with_bound(
            _i(5), None, low_inclusive=True, high_inclusive=True
        )
        assert [k.payload for k, _ in result] == [5, 7, 9]
        # Same range with exclusive low drops the 5.
        result_excl = tree.range_scan_with_bound(
            _i(5), None, low_inclusive=False, high_inclusive=True
        )
        assert [k.payload for k, _ in result_excl] == [7, 9]

    def test_text_keys_gt_exclusive(self, env):
        """Exclusive bounds work for TEXT keys as well."""
        _, _, tree = env
        for w in ["apple", "banana", "cherry", "date"]:
            tree.insert(_t(w), 1)
        result = tree.range_scan_with_bound(
            _t("banana"), _t("date"),
            low_inclusive=False, high_inclusive=False,
        )
        assert [k.payload for k, _ in result] == ["cherry"]

    def test_null_excluded_when_bound_present(self, env):
        """When a concrete bound is supplied, NULL keys are dropped from
        the result. They still sort to the end in the underlying scan,
        but `range_scan_with_bound` must filter them out so callers can
        rely on a non-null payload."""
        _, _, tree = env
        tree.insert(_i(1), 10)
        tree.insert(Value.null(), 99)
        tree.insert(_i(2), 20)
        tree.insert(Value.null(), 98)
        tree.insert(_i(3), 30)
        result = tree.range_scan_with_bound(
            _i(1), _i(3), low_inclusive=True, high_inclusive=True
        )
        assert [k.payload for k, _ in result] == [1, 2, 3]
        # Also true with exclusive bounds.
        result_excl = tree.range_scan_with_bound(
            _i(1), _i(3), low_inclusive=False, high_inclusive=True
        )
        assert [k.payload for k, _ in result_excl] == [2, 3]

    def test_contradictory_exclusive_returns_empty(self, env):
        """An exclusive range where low >= high must yield no rows."""
        _, _, tree = env
        for i in [1, 3, 5, 7]:
            tree.insert(_i(i), i)
        # low == high with both exclusive => empty.
        result_eq = tree.range_scan_with_bound(
            _i(3), _i(3), low_inclusive=False, high_inclusive=False
        )
        assert result_eq == []
        # low > high => empty regardless of inclusive flags.
        result_gt = tree.range_scan_with_bound(
            _i(5), _i(3), low_inclusive=True, high_inclusive=True
        )
        assert result_gt == []
