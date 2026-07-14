# Tasks: init-tinydb

## 1. Project Setup

- [x] 1.1 Create `tinydb/` package skeleton: `__init__.py`, `__main__.py`, `errors.py`, empty subpackages `parser/`, `types/`, `storage/`, `catalog/`, `executor/`, `index/`, `txn/`, `cli/`
- [x] 1.2 Create `tests/` directory with `pytest` configuration and `conftest.py` providing a `tmp_db` fixture that creates a temp `.db` file per test
- [x] 1.3 Add `pyproject.toml` declaring the `tinydb` package, Python ≥ 3.10, zero runtime dependencies, dev deps `pytest` + `pytest-cov`
- [x] 1.4 Define exception hierarchy in `tinydb/errors.py`: `TinyDBError`, `ParseError`, `ConstraintError`, `TypeError` (alias to avoid stdlib clash via `TypeMismatchError`), `StorageError`, `TransactionError`

## 2. Type System

- [x] 2.1 Implement `Value` representation in `tinydb/types/value.py` with `tag ∈ {INT, FLOAT, TEXT, BOOL, NULL}` and comparison helpers (`__eq__`, `__lt__`, etc.)
- [x] 2.2 Implement serialization in `tinydb/types/serialize.py`: INT/FLOAT fixed 8 bytes, BOOL 1 byte, TEXT `[len:u32][bytes]`, NULL 0 bytes; round-trip helpers
- [x] 2.3 Implement type validation in `tinydb/types/check.py`: `coerce(literal, declared_type) -> Value` raising `TypeMismatchError` on mismatch; INT/FLOAT numeric promotion rules
- [x] 2.4 Add unit tests for type system covering happy paths, mismatches, NULL semantics, and round-trip serialization (multi-byte text, edge numerics)

## 3. Storage Engine

- [x] 3.1 Define on-disk constants and page structures in `tinydb/storage/page.py`: `PAGE_SIZE = 4096`, `FileHeader` dataclass, page-type enum, `Page` container with raw bytes + metadata
- [x] 3.2 Implement `DiskManager` in `tinydb/storage/disk.py`: open/create file, read/write pages by id, atomic grow, magic check, version check
- [x] 3.3 Implement `BufferPool` in `tinydb/storage/buffer.py`: LRU eviction, dirty tracking, flush-before-evict, in-memory page table
- [x] 3.4 Implement free-list management in `tinydb/storage/freelist.py`: `allocate()` reuses freed ids, `free(id)` returns to pool, header sync
- [x] 3.5 Add unit tests using a temp file: open/create, write/read pages, eviction invariants, free-list reuse, magic-byte rejection

## 4. Catalog

- [x] 4.1 Define `TableMeta` and `ColumnMeta` and `IndexMeta` dataclasses in `tinydb/catalog/schema.py` with (de)serialization
- [x] 4.2 Implement `Catalog` in `tinydb/catalog/catalog.py`: load from catalog root page on open, persist on close, methods `create_table`, `drop_table`, `get_table`, `list_tables`, `add_index`, `drop_index`
- [x] 4.3 Add unit tests for catalog persistence across reopen, duplicate-table rejection, missing-table errors

## 5. SQL Parser

- [x] 5.1 Implement lexer in `tinydb/parser/lexer.py`: keywords (case-insensitive), identifiers, integer/float/string/bool literals, operators, `;`, `(`, `)`, `,`; emit `Token(kind, lexeme, line, col)`
- [x] 5.2 Define AST node classes in `tinydb/parser/ast.py`: `Stmt` subclasses (`CreateTableStmt`, `DropTableStmt`, `InsertStmt`, `SelectStmt`, `UpdateStmt`, `DeleteStmt`, `BeginStmt`, `CommitStmt`, `RollbackStmt`), expressions (`Literal`, `ColumnRef`, `BinaryOp`, `UnaryOp`, `FunctionCall`, `Star`)
- [x] 5.3 Implement recursive-descent parser in `tinydb/parser/parser.py`: entry point `parse(sql) -> list[Stmt]`; one parse function per statement; raise `ParseError` with position on failure
- [x] 5.4 Implement expression parser with correct precedence: `OR` < `AND` < comparison `<`/`>`/`<=`/`>=`/`=`/`<>` < additive < multiplicative < unary < primary
- [x] 5.5 Add unit tests for each statement type covering every scenario in `specs/sql-parser/spec.md`

## 6. B-tree Index

- [x] 6.1 Implement `BPlusTree` skeleton in `tinydb/index/btree.py`: constants for `m` (order), internal/leaf node layouts, page-encoded `find_child` / `find_key`
- [x] 6.2 Implement `insert(key, value_ptr)` with node split and recursive propagation; cover root-split case (new root page allocated)
- [x] 6.3 Implement `point_lookup(key) -> value_ptr | None`
- [x] 6.4 Implement `range_scan(low, high)` iterator using the leaf `next_leaf` chain
- [x] 6.5 Implement `delete(key)` with borrow/merge rebalancing up to the root
- [x] 6.6 Add invariant tests: ordered keys, leaf chain closed, uniform depth across leaves, property-based test with 1000 random inserts/deletes

## 7. Executor

- [x] 7.1 Define Volcano operator base class in `tinydb/executor/operator.py`: `open()`, `next() -> Row | None`, `close()`
- [x] 7.2 Implement `SeqScan` and `IndexScan` operators reading from heap pages or B-tree respectively
- [x] 7.3 Implement `Filter`, `Project`, `Sort`, `Limit`, `Offset` operators
- [x] 7.4 Implement `Aggregate` operator supporting `COUNT`, `SUM`, `AVG` with optional `GROUP BY`
- [x] 7.5 Implement write operators `Insert`, `Update`, `Delete`, `CreateTable`, `DropTable`
- [x] 7.6 Implement planner in `tinydb/executor/planner.py`: walk AST, choose `IndexScan` vs `SeqScan` based on available indexes, build operator tree
- [x] 7.7 Wire `Database.execute(sql)` to: parse → plan → execute → return `ResultSet` (`columns`, `rows`, `rows_affected`)
- [x] 7.8 Add unit tests for each operator and end-to-end SQL scenarios from `specs/query-execution/spec.md`

## 8. Transaction Manager / WAL

- [x] 8.1 Implement WAL record format and append-only writer in `tinydb/txn/wal.py`: `<dbfile>.wal`, fsync on commit, log sequence numbers
- [x] 8.2 Implement `TransactionManager` in `tinydb/txn/manager.py`: track current txn, single-writer enforcement, `begin/commit/rollback`
- [x] 8.3 Implement redo/undo during data-page writes: write WAL record before mutating the in-memory page; discard page changes on rollback
- [x] 8.4 Implement recovery in `tinydb/txn/recovery.py`: on open, scan WAL to determine last checkpoint and replay committed txns, discard uncommitted
- [x] 8.5 Implement checkpoint operation: flush all dirty pages, record `CHECKPOINT` log entry, truncate WAL
- [x] 8.6 Add unit tests for: explicit transaction commit/rollback, atomicity across multiple writes, recovery after simulated crash (drop data file but keep WAL)

## 9. CLI / REPL

- [x] 9.1 Implement `python -m tinydb <dbfile>` entry in `tinydb/__main__.py` validating args and starting the REPL
- [x] 9.2 Implement statement buffer in `tinydb/cli/repl.py`: accumulate lines until `;`, hand off to `Database.execute`, print results
- [x] 9.3 Implement tabular formatter in `tinydb/cli/format.py` (column-aligned, header row, row count summary)
- [x] 9.4 Implement meta-commands `.tables`, `.schema <name>`, `.exit`, `.quit`; wire `.exit`/`.quit` to clean DB close
- [x] 9.5 Add end-to-end CLI tests using `subprocess` or direct REPL driver that exercise SELECT formatting, multi-line input, error handling, and meta-commands

## 10. Integration & Verification

- [x] 10.1 Write integration tests in `tests/integration/` that exercise full SQL workflows (create → insert → select → update → delete → drop) against a real file-backed database
- [x] 10.2 Verify coverage ≥ 80% across `tinydb/`; address gaps in error paths and edge cases
- [x] 10.3 Write `README.md` at project root: install, quickstart, supported SQL subset, file format overview, CLI usage, pointer to `openspec/changes/init-tinydb/` for the change rationale
- [x] 10.4 Run `openspec validate init-tinydb --strict` and resolve any reported issues until clean
- [x] 10.5 Run `openspec archive init-tinydb --yes` (only after all tasks complete and `apply` is done)