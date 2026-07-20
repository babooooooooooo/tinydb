"""B+ tree index on top of the page store.

Layout (each node = one 4 KiB page):

Leaf page (PageType.BTREE_LEAF), payload after the 13-byte in-page header:
    [num_entries : u16] [entry_0] [entry_1] ... [entry_{n-1}]
    entry_i      := serialized key  || value_ptr : u32
    next         : page id of the next leaf (range-scan chain) or 0
    prev         : page id of the previous leaf or 0

Internal page (PageType.BTREE_INTERNAL), payload:
    [num_children : u16] [child_0 : u32] [key_0] [child_1 : u32] [key_1] ...
        ... [key_{n-1}] [child_n : u32]
    next / prev   : unused (set to 0)

Order ``m`` controls how many children an internal node may hold; with
``m = 64`` a full internal node stays well under 4 KiB. The split point is
the median of an over-full node. ``BPlusTree`` operates on disk pages via
``BufferPool`` so the in-memory footprint is bounded.

Public API:
    tree = BPlusTree(pool, freelist, root_page_id)
    tree.insert(key, value_ptr)
    ptr = tree.point_lookup(key)
    for k, v in tree.range_scan(low, high): ...
    tree.delete(key)
"""

from __future__ import annotations

from typing import Optional

from tinydb.errors import StorageError
from tinydb.storage.buffer import BufferPool
from tinydb.storage.freelist import FreeList
from tinydb.storage.page import PAGE_HEADER_SIZE, PAGE_SIZE, Page, PageType
from tinydb.types import Tag, Value
from tinydb.types.serialize import deserialize, serialize


# ---- B+ tree configuration -----------------------------------------------

# Fan-out: max children per internal node. Chosen so that an internal node
# with m=64 children storing INT keys fits comfortably under 4 KiB
# (64 * (4 + 1 + 8 + 4) ≈ 1088 bytes).
ORDER: int = 64
# Per-node byte cost approximations used by ``_entry_size``.
_INT_KEY_SIZE = 1 + 8               # tag + payload (8)
_FLOAT_KEY_SIZE = 1 + 8
_BOOL_KEY_SIZE = 1 + 1
_NULL_KEY_SIZE = 1
_TEXT_KEY_OVERHEAD = 1 + 4          # tag + length
_INT64_KEY_SIZE = 1 + 8              # VARCHAR/CHAR/DECIMAL share this


def _entry_size(key: Value) -> int:
    """Bytes an on-disk leaf entry occupies for ``key`` (excludes ptr)."""
    if key.tag is Tag.INT:
        return _INT_KEY_SIZE
    if key.tag is Tag.FLOAT:
        return _FLOAT_KEY_SIZE
    if key.tag is Tag.BOOL:
        return _BOOL_KEY_SIZE
    if key.tag is Tag.NULL:
        return _NULL_KEY_SIZE
    if key.tag in (Tag.TEXT, Tag.VARCHAR, Tag.CHAR, Tag.DECIMAL):
        return _TEXT_KEY_OVERHEAD + len(key.payload.encode("utf-8"))
    if key.tag in (Tag.DATE, Tag.TIME, Tag.TIMESTAMP, Tag.SMALLINT, Tag.BIGINT):
        return _INT64_KEY_SIZE
    raise StorageError(f"unknown tag {key.tag!r}")


def _internal_overhead() -> int:
    """Fixed bytes an internal node always uses: header + num_children."""
    return 2  # num_children : u16


def _value_ptr_size() -> int:
    return 4


# ---- helpers --------------------------------------------------------------


def _load_leaf_entries(page: Page) -> list[tuple[Value, int]]:
    """Read all (key, value_ptr) pairs from a leaf page, in order."""
    data = bytes(page.data)
    (num,) = _u16(data, PAGE_HEADER_SIZE)
    entries: list[tuple[Value, int]] = []
    offset = PAGE_HEADER_SIZE + 2
    for _ in range(num):
        key, offset = deserialize(data, offset)
        (ptr,) = _u32(data, offset)
        offset += 4
        entries.append((key, ptr))
    return entries


def _write_leaf_entries(page: Page, entries: list[tuple[Value, int]]) -> None:
    """Rewrite the leaf page with the given ordered entries."""
    payload = bytearray()
    payload += _u16_pack(len(entries))
    for key, ptr in entries:
        payload += serialize(key) + _u32_pack(ptr)
    page.data[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + len(payload)] = payload
    page.free_offset = PAGE_HEADER_SIZE + len(payload)
    page.num_slots = len(entries)
    # next/prev preserved by caller.
    page.dirty = True


def _load_internal(page: Page) -> tuple[list[int], list[Value]]:
    """Return (children, keys) from an internal node.

    children has length n+1; keys has length n.
    """
    data = bytes(page.data)
    (num_children,) = _u16(data, PAGE_HEADER_SIZE)
    children: list[int] = []
    keys: list[Value] = []
    offset = PAGE_HEADER_SIZE + 2
    (c0,) = _u32(data, offset)
    children.append(c0)
    offset += 4
    for _ in range(num_children - 1):
        key, offset = deserialize(data, offset)
        keys.append(key)
        (c,) = _u32(data, offset)
        children.append(c)
        offset += 4
    return children, keys


def _write_internal(
    page: Page, children: list[int], keys: list[Value]
) -> None:
    """Rewrite an internal node with the given children and separator keys."""
    payload = bytearray()
    payload += _u16_pack(len(children))
    payload += _u32_pack(children[0])
    for i, key in enumerate(keys):
        payload += serialize(key)
        payload += _u32_pack(children[i + 1])
    page.data[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + len(payload)] = payload
    page.free_offset = PAGE_HEADER_SIZE + len(payload)
    page.num_slots = len(keys)
    page.next = 0
    page.prev = 0
    page.dirty = True


def _u16(data: bytes, offset: int) -> tuple[int]:
    import struct

    return struct.unpack_from("<H", data, offset)


def _u32(data: bytes, offset: int) -> tuple[int]:
    import struct

    return struct.unpack_from("<I", data, offset)


def _u16_pack(v: int) -> bytes:
    import struct

    return struct.pack("<H", v)


def _u32_pack(v: int) -> bytes:
    import struct

    return struct.pack("<I", v)


def _u16_put(buf: bytearray, offset: int, v: int) -> None:
    import struct

    struct.pack_into("<H", buf, offset, v)


# ---- B+ tree --------------------------------------------------------------


class BPlusTree:
    """A B+ tree where each node occupies one persistent page.

    Keys are ``Value`` objects (comparable). Pointers are opaque ``int``
    row identifiers (typically the byte offset within heap pages).
    """

    HEADER_LEAF_FANOUT_HINT = ORDER  # cosmetic; not enforced at format level

    def __init__(
        self,
        pool: BufferPool,
        freelist: FreeList,
        root_page_id: int = 0,
        txn=None,
    ) -> None:
        self.pool = pool
        self.freelist = freelist
        self.root_page_id = root_page_id
        # Optional transaction manager: when set, every page the tree
        # fetches is registered with the txn so ROLLBACK can restore
        # the pre-image. Without this, an INSERT-in-txn followed by
        # ROLLBACK would leave the index pointing at the now-freed row
        # offset, and a later IndexScan would decode garbage there.
        self.txn = txn

    # ---- lifecycle --------------------------------------------------------

    def ensure_root(self) -> Page:
        """Pin and return the root leaf page; create an empty one if needed."""
        if self.root_page_id == 0:
            leaf = self.freelist.allocate(PageType.BTREE_LEAF)
            self._init_empty_leaf(leaf)
            self.root_page_id = leaf.page_id
            return self._pin(leaf.page_id)
        return self._pin(self.root_page_id)

    def _init_empty_leaf(self, leaf: Page) -> None:
        payload = _u16_pack(0)  # num_entries = 0
        leaf.data[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + len(payload)] = payload
        leaf.free_offset = PAGE_HEADER_SIZE + len(payload)
        leaf.num_slots = 0
        leaf.next = 0
        leaf.prev = 0
        leaf.dirty = True
        self.pool.disk.write_page(leaf)

    # ---- public API -------------------------------------------------------

    def insert(self, key: Value, value_ptr: int) -> None:
        # If tree is empty, root is the (empty) leaf.
        root = self.ensure_root()
        # Walk down to leaf, keeping every internal node pinned in `path`.
        path: list[tuple[Page, int]] = []  # (internal_page, child_idx_used)
        node = root
        while node.page_type is PageType.BTREE_INTERNAL:
            children, keys = _load_internal(node)
            child_idx = self._descend_child(keys, key, len(children))
            path.append((node, child_idx))
            node = self._pin(children[child_idx])
        # node is now a leaf.
        leaf = node
        entries = _load_leaf_entries(leaf)
        # Replace existing key if present.
        pos = self._lower_bound_entries(entries, key)
        if pos < len(entries) and _key_eq(entries[pos][0], key):
            entries[pos] = (key, value_ptr)
        else:
            entries.insert(pos, (key, value_ptr))
        # Check overflow.
        if self._leaf_will_fit(entries):
            _write_leaf_entries(leaf, entries)
            # Unpin leaf + every internal in the path.
            self._unpin_all([leaf] + [p for p, _ in path], dirty=True)
            return
        # Split the leaf.
        left, right, median = self._split_leaf(entries)
        _write_leaf_entries(leaf, left)
        right_page = self._new_leaf()
        _write_leaf_entries(right_page, right)
        # Link leaves.
        old_next = leaf.next
        right_page.next = old_next
        right_page.prev = leaf.page_id
        leaf.next = right_page.page_id
        pinned_aux: list[Page] = []
        if old_next != 0:
            old_next_page = self._pin(old_next)
            old_next_page.prev = right_page.page_id
            pinned_aux.append(old_next_page)
        # Insert median into parent (or grow root).
        new_pages: list[Page] = []
        if path:
            parent, child_idx = path[-1]
            self._insert_into_parent(
                parent,
                child_idx,
                median,
                right_page.page_id,
                path[:-1],
                new_pages,
            )
        else:
            self._grow_root(leaf.page_id, median, right_page.page_id)
        # Unpin leaf + new leaf + path internals + aux pages + new nodes.
        self._unpin_all(
            [leaf, right_page]
            + [p for p, _ in path]
            + pinned_aux
            + new_pages,
            dirty=True,
        )

    def point_lookup(self, key: Value) -> Optional[int]:
        if self.root_page_id == 0:
            return None
        page = self._pin(self.root_page_id)
        try:
            while page.page_type is PageType.BTREE_INTERNAL:
                children, keys = _load_internal(page)
                child_idx = self._descend_child(keys, key, len(children))
                self.pool.unpin_page(page.page_id, dirty=page.dirty)
                page = self._pin(children[child_idx])
            entries = _load_leaf_entries(page)
            pos = self._lower_bound_entries(entries, key)
            if pos < len(entries) and _key_eq(entries[pos][0], key):
                return entries[pos][1]
            return None
        finally:
            self.pool.unpin_page(page.page_id, dirty=page.dirty)

    def range_scan(
        self, low: Value | None, high: Value | None
    ) -> list[tuple[Value, int]]:
        """Yield (key, value_ptr) in [low, high] in ascending order.

        Both bounds are inclusive. None means open-ended.
        """
        if self.root_page_id == 0:
            return []
        # Find first leaf containing or after ``low``.
        page = self._pin(self.root_page_id)
        try:
            while page.page_type is PageType.BTREE_INTERNAL:
                children, keys = _load_internal(page)
                if low is None:
                    child_idx = 0
                else:
                    child_idx = self._descend_child(keys, low, len(children))
                self.pool.unpin_page(page.page_id, dirty=page.dirty)
                page = self._pin(children[child_idx])
            out: list[tuple[Value, int]] = []
            while True:
                entries = _load_leaf_entries(page)
                for k, v in entries:
                    if low is not None and _key_lt(k, low):
                        continue
                    if high is not None and _key_gt(k, high):
                        return out
                    out.append((k, v))
                if page.next == 0:
                    return out
                nxt = page.next
                self.pool.unpin_page(page.page_id, dirty=page.dirty)
                page = self._pin(nxt)
        finally:
            self.pool.unpin_page(page.page_id, dirty=page.dirty)

    def range_scan_with_bound(
        self,
        low: Value | None,
        high: Value | None,
        *,
        low_inclusive: bool = True,
        high_inclusive: bool = True,
    ) -> list[tuple[Value, int]]:
        """Yield (key, value_ptr) within ``[low, high]`` honoring inclusive flags.

        ``low_inclusive=False`` makes the lower bound strict (key > low);
        ``high_inclusive=False`` makes the upper bound strict (key < high).
        A ``None`` bound is open-ended regardless of its inclusive flag.

        NULL keys are excluded from the result whenever a concrete bound is
        supplied, so callers using this method see a non-null payload.
        The legacy ``range_scan`` (always inclusive, NULLs preserved when
        in range) is left unchanged for callers that depend on it.
        """
        # 1) Get the inclusive [low, high] window via the existing scan.
        #    NULLs that fall inside this window (e.g. low is concrete but
        #    high is open) are dropped in step 2.
        candidates = self.range_scan(low, high)
        # 2) Tighten the window according to the inclusive flags, and
        #    strip NULLs whenever any concrete bound is supplied.
        has_bound = low is not None or high is not None
        out: list[tuple[Value, int]] = []
        for k, v in candidates:
            if has_bound and k.tag is Tag.NULL:
                continue
            if low is not None and not low_inclusive and _key_eq(k, low):
                continue
            if high is not None and not high_inclusive and _key_eq(k, high):
                continue
            out.append((k, v))
        return out

    def delete(self, key: Value) -> bool:
        """Delete ``key`` from the tree. Returns True if a key was removed."""
        if self.root_page_id == 0:
            return False
        # Find leaf containing the key, keeping ancestors pinned in path.
        path: list[tuple[Page, int]] = []
        page = self._pin(self.root_page_id)
        try:
            while page.page_type is PageType.BTREE_INTERNAL:
                children, keys = _load_internal(page)
                child_idx = self._descend_child(keys, key, len(children))
                path.append((page, child_idx))
                page = self._pin(children[child_idx])
            entries = _load_leaf_entries(page)
            pos = self._lower_bound_entries(entries, key)
            if pos >= len(entries) or not _key_eq(entries[pos][0], key):
                self._unpin_all([page] + [p for p, _ in path], dirty=False)
                return False
            entries.pop(pos)
            _write_leaf_entries(page, entries)
            # Rebalance up if leaf now below minimum fill.
            to_unpin: list[Page] = []
            if len(entries) < self._min_leaf_fill():
                self._rebalance_leaf(path, page, to_unpin)
            else:
                to_unpin.append(page)
            # Always unpin all the path ancestors we held pinned.
            for p, _ in path:
                if p not in to_unpin:
                    to_unpin.append(p)
            self._unpin_all(to_unpin, dirty=True)
            return True
        except BaseException:
            self._unpin_all([page] + [p for p, _ in path], dirty=False)
            raise

    # ---- internals --------------------------------------------------------

    @staticmethod
    def _descend_child(
        seps: list[Value], key: Value, n_children: int
    ) -> int:
        """Return the child index to descend into for ``key``.

        Convention: ``seps[i]`` is the first key of children[i+1]; a key
        equal to a separator lives in the RIGHT child (since separators are
        copy-ups). Returns 0 <= idx <= n_children-1.
        """
        idx = BPlusTree._lower_bound(seps, key)
        if idx < len(seps) and _key_eq(seps[idx], key):
            return idx + 1
        return idx

    def _pin(self, page_id: int) -> Page:
        page = self.pool.fetch_page(page_id)
        # Snapshot pre-image for rollback. We log every fetched page,
        # including those only read (lookup/scan): the redundant
        # pre-image is byte-identical to the post, so rollback
        # restoration is a no-op for them.
        if self.txn is not None and self.txn.in_transaction:
            self.txn.log_page_write(page)
        return page

    def _unpin_all(self, pages: list[Page], dirty: bool = True) -> None:
        for p in pages:
            self.pool.unpin_page(p.page_id, dirty=dirty or p.dirty)

    def _new_leaf(self) -> Page:
        leaf = self.freelist.allocate(PageType.BTREE_LEAF)
        self._init_empty_leaf(leaf)
        # Caller is responsible for unpinning.
        return self.pool.fetch_page(leaf.page_id)

    def _new_internal(self) -> Page:
        node = self.freelist.allocate(PageType.BTREE_INTERNAL)
        # Will be filled in by _write_internal.
        return self.pool.fetch_page(node.page_id)

    @staticmethod
    def _leaf_entry_bytes(entries: list[tuple[Value, int]]) -> int:
        total = 2  # num_entries u16
        for k, v in entries:
            total += _entry_size(k) + 4
        return total

    def _leaf_will_fit(self, entries: list[tuple[Value, int]]) -> bool:
        return self._leaf_entry_bytes(entries) + PAGE_HEADER_SIZE <= PAGE_SIZE

    @staticmethod
    def _min_leaf_fill() -> int:
        # For order m we keep at least floor((m-1)/2) keys in a leaf;
        # m is the FANOUT count of children in an internal node, but
        # leaves hold m-1 entries (the average ratio works).
        return max(1, (ORDER - 1) // 2)

    @staticmethod
    def _min_internal_keys() -> int:
        return max(1, ORDER // 2 - 1)

    def _split_leaf(
        self, entries: list[tuple[Value, int]]
    ) -> tuple[list[tuple[Value, int]], list[tuple[Value, int]], Value]:
        mid = len(entries) // 2
        left = entries[:mid]
        right = entries[mid:]
        median = right[0][0]  # copy-up: median key goes up but stays in right
        return left, right, median

    def _split_internal(
        self, children: list[int], keys: list[Value]
    ) -> tuple[list[int], list[Value], list[int], list[Value], Value]:
        mid = len(keys) // 2
        left_children = children[: mid + 1]
        left_keys = keys[:mid]
        right_children = children[mid + 1 :]
        right_keys = keys[mid + 1 :]
        median = keys[mid]
        return left_children, left_keys, right_children, right_keys, median

    def _grow_root(
        self, left_child: int, median: Value, right_child: int
    ) -> Page:
        """Allocate a new root holding (left_child, median, right_child).

        The new root is returned pinned; caller is responsible for unpinning.
        """
        new_root = self._new_internal()
        _write_internal(new_root, [left_child, right_child], [median])
        self.root_page_id = new_root.page_id
        return new_root

    def _insert_into_parent(
        self,
        parent: Page,
        child_idx: int,
        key: Value,
        right_child_id: int,
        ancestors: list[tuple[Page, int]],
        new_pages: list[Page],
    ) -> None:
        """Insert ``key`` as a separator BEFORE children[child_idx+1] in
        ``parent``, putting ``right_child_id`` at children[child_idx+1]
        (shifting the old child right by one).

        ``parent`` and pages in ``ancestors`` are pinned by the caller; new
        pages allocated here are appended to ``new_pages`` for caller unpin.
        """
        children, keys = _load_internal(parent)
        # Splice in the new separator and the right child.
        children.insert(child_idx + 1, right_child_id)
        keys.insert(child_idx, key)
        if self._internal_will_fit(children, keys):
            _write_internal(parent, children, keys)
            return
        # Split internal node.
        left_c, left_k, right_c, right_k, median = self._split_internal(
            children, keys
        )
        _write_internal(parent, left_c, left_k)
        new_node = self._new_internal()
        _write_internal(new_node, right_c, right_k)
        new_pages.append(new_node)
        if ancestors:
            grandparent, gp_idx = ancestors[-1]
            self._insert_into_parent(
                grandparent,
                gp_idx,
                median,
                new_node.page_id,
                ancestors[:-1],
                new_pages,
            )
        else:
            new_pages.append(
                self._grow_root(parent.page_id, median, new_node.page_id)
            )

    @staticmethod
    def _internal_will_fit(children: list[int], keys: list[Value]) -> bool:
        size = 2 + 4 * len(children)  # num_children + child pointers
        for k in keys:
            size += _entry_size(k)
        return size + PAGE_HEADER_SIZE <= PAGE_SIZE

    def _rebalance_leaf(
        self,
        path: list[tuple[Page, int]],
        leaf: Page,
        to_unpin: list[Page],
    ) -> None:
        """Rebalance ``leaf`` after a deletion; append all unpinned pages to
        ``to_unpin``. The caller is responsible for actually unpinning.

        ``leaf`` and every page in ``path`` are pinned on entry; this method
        may pin additional sibling pages but does not unpin anything itself.
        """
        if not path:
            entries = _load_leaf_entries(leaf)
            if not entries:
                # Tree becomes empty; we don't free the page here.
                self.root_page_id = 0
            to_unpin.append(leaf)
            return
        parent, idx = path[-1]
        children, keys = _load_internal(parent)
        left_id = children[idx - 1] if idx > 0 else 0
        right_id = children[idx + 1] if idx + 1 < len(children) else 0
        # Try borrowing from a sibling.
        if left_id != 0:
            left_page = self._pin(left_id)
            left_entries = _load_leaf_entries(left_page)
            if len(left_entries) > self._min_leaf_fill():
                entries = _load_leaf_entries(leaf)
                borrowed = left_entries.pop()
                entries.insert(0, borrowed)
                keys[idx - 1] = entries[0][0]
                _write_leaf_entries(left_page, left_entries)
                _write_leaf_entries(leaf, entries)
                _write_internal(parent, children, keys)
                to_unpin.extend([left_page, leaf, parent])
                return
            to_unpin.append(left_page)
        if right_id != 0:
            right_page = self._pin(right_id)
            right_entries = _load_leaf_entries(right_page)
            if len(right_entries) > self._min_leaf_fill():
                entries = _load_leaf_entries(leaf)
                borrowed = right_entries.pop(0)
                entries.append(borrowed)
                keys[idx] = right_entries[0][0]
                _write_leaf_entries(right_page, right_entries)
                _write_leaf_entries(leaf, entries)
                _write_internal(parent, children, keys)
                to_unpin.extend([right_page, leaf, parent])
                return
            to_unpin.append(right_page)
        # Cannot borrow: merge with a sibling.
        if left_id != 0:
            left_page = self._pin(left_id)
            left_entries = _load_leaf_entries(left_page)
            entries = _load_leaf_entries(leaf)
            merged = left_entries + entries
            _write_leaf_entries(left_page, merged)
            nxt = leaf.next
            left_page.next = nxt
            if nxt != 0:
                nxt_page = self._pin(nxt)
                nxt_page.prev = left_page.page_id
                to_unpin.append(nxt_page)
            children.pop(idx)
            keys.pop(idx - 1)
            # `leaf` is now obsolete; return it to the freelist. Do NOT
            # add it to to_unpin — freelist.free discards it from the pool.
            to_unpin.append(left_page)
            self.freelist.free(leaf.page_id)
            self._after_child_removed(parent, children, keys, path[:-1], to_unpin)
            return
        if right_id != 0:
            right_page = self._pin(right_id)
            right_entries = _load_leaf_entries(right_page)
            entries = _load_leaf_entries(leaf)
            merged = entries + right_entries
            _write_leaf_entries(leaf, merged)
            nxt = right_page.next
            leaf.next = nxt
            if nxt != 0:
                nxt_page = self._pin(nxt)
                nxt_page.prev = leaf.page_id
                to_unpin.append(nxt_page)
            children.pop(idx + 1)
            keys.pop(idx)
            # `right_page` is now obsolete; return it to the freelist.
            to_unpin.append(leaf)
            self.freelist.free(right_page.page_id)
            self._after_child_removed(parent, children, keys, path[:-1], to_unpin)
            return
        # Only child of parent; collapse the root.
        to_unpin.extend([leaf, parent])
        if leaf.page_type is PageType.BTREE_LEAF:
            self.root_page_id = leaf.page_id
            self.freelist.free(parent.page_id)

    def _after_child_removed(
        self,
        parent: Page,
        children: list[int],
        keys: list[Value],
        ancestors: list[tuple[Page, int]],
        to_unpin: list[Page],
    ) -> None:
        if not children:
            self.freelist.free(parent.page_id)
            self.root_page_id = 0
            return
        # Naive policy: always write and never borrow/merge at the internal
        # level. The tree remains correct; only space utilization degrades.
        _write_internal(parent, children, keys)
        to_unpin.append(parent)

    # ---- comparison helpers ----------------------------------------------

    @staticmethod
    def _lower_bound(keys: list[Value], key: Value) -> int:
        lo, hi = 0, len(keys)
        while lo < hi:
            mid = (lo + hi) // 2
            if _key_lt(keys[mid], key):
                lo = mid + 1
            else:
                hi = mid
        return lo

    @staticmethod
    def _lower_bound_entries(
        entries: list[tuple[Value, int]], key: Value
    ) -> int:
        lo, hi = 0, len(entries)
        while lo < hi:
            mid = (lo + hi) // 2
            if _key_lt(entries[mid][0], key):
                lo = mid + 1
            else:
                hi = mid
        return lo


# ---- module-level key comparison helpers ----------------------------------


def _key_lt(a: Value, b: Value) -> bool:
    """Strict less-than for index ordering.

    NULL sorts as the LARGEST value (Postgres convention) so ASC scans put
    NULLs at the end. Mixed non-numeric tags are not ordered; treat as not
    less-than (deterministic, but the caller should not rely on order).
    """
    if a.tag is Tag.NULL:
        return False  # NULL is larger than anything except another NULL
    if b.tag is Tag.NULL:
        return True
    if a.tag is b.tag:
        return a.payload < b.payload
    if a.tag in (Tag.INT, Tag.FLOAT) and b.tag in (Tag.INT, Tag.FLOAT):
        return float(a.payload) < float(b.payload)
    return False


def _key_gt(a: Value, b: Value) -> bool:
    return _key_lt(b, a)


def _key_eq(a: Value, b: Value) -> bool:
    if a.tag is Tag.NULL or b.tag is Tag.NULL:
        return False  # NULL is never equal to anything, even another NULL
    if a.tag is b.tag:
        return a.payload == b.payload
    if a.tag in (Tag.INT, Tag.FLOAT) and b.tag in (Tag.INT, Tag.FLOAT):
        return float(a.payload) == float(b.payload)
    return False