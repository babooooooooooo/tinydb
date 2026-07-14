# btree-index Specification

## Purpose
TBD - created by archiving change init-tinydb. Update Purpose after archive.
## Requirements
### Requirement: B+ tree structure
The system SHALL implement a B+ tree index where each node occupies one storage page, internal nodes hold keys and child page ids, and leaf nodes hold key-pointer pairs and a `next_leaf` pointer for sequential range scans.

#### Scenario: Tree shape invariants
- **WHEN** a freshly-built index with 1000 random keys is introspected
- **THEN** all internal nodes have between `⌈m/2⌉` and `m` children; all leaves are at the same depth; the leaf chain is a single forward-linked list

### Requirement: Insert into index
The system SHALL insert `(key, value_ptr)` into the B+ tree; on overflow, the affected node SHALL split and the new median SHALL be propagated to the parent (recursive split up to the root).

#### Scenario: Insert unique keys
- **WHEN** 1000 unique keys are inserted
- **THEN** `point_lookup` returns the correct value pointer for every key

#### Scenario: Root split grows tree height
- **WHEN** enough keys are inserted to force a root split
- **THEN** the tree depth increases by exactly 1 and all existing keys remain point-queryable

### Requirement: Equality point lookup
The system SHALL support `point_lookup(key) → value_ptr | None` with O(log n) cost.

#### Scenario: Point lookup hit
- **WHEN** a key that was previously inserted is looked up
- **THEN** the original value pointer is returned

#### Scenario: Point lookup miss
- **WHEN** a key that was never inserted is looked up
- **THEN** `None` is returned

### Requirement: Range scan
The system SHALL support `range_scan(low, high) → iterator[(key, value_ptr)]` returning all keys in `[low, high]` in ascending order, using the leaf chain (no upward traversal).

#### Scenario: Range scan
- **WHEN** keys 1..1000 are inserted and the user scans `[100, 110]`
- **THEN** the iterator yields exactly 11 entries in order 100, 101, ..., 110

### Requirement: Delete from index
The system SHALL delete `(key, value_ptr)` and rebalance the tree; underflowing nodes SHALL borrow from siblings or merge, possibly propagating changes up to the root.

#### Scenario: Delete causes merge
- **WHEN** enough keys are deleted to force a leaf merge
- **THEN** after the operation the tree still satisfies the B+ tree invariants and remaining keys are point-queryable

### Requirement: Index tied to storage
The system SHALL persist the B+ tree's root page id in the catalog and reload it on open; the root SHALL remain the same page id across reopens as long as no structural change requires a new root.

#### Scenario: Index survives reopen
- **WHEN** a table with an index is closed and reopened
- **THEN** lookups via the index return correct results and no full rebuild is required

