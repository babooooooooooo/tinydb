# tinydb

A lightweight embedded relational database in pure Python — pages on
disk, a B+ tree for indexing, a Volcano-style executor, and a
single-writer WAL for crash safety. Zero runtime dependencies.

## Why

A teaching/portfolio database that fits in a few thousand lines:
read the whole thing and you can see how a real DB handles pages,
indexes, parsing, planning, and recovery.

## Install

```bash
pip install -e .[dev]
```

## Quickstart (Python API)

```python
from tinydb import Database

with Database("example.db") as db:
    db.execute("CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL)")
    db.execute("INSERT INTO users VALUES (1, 'alice')")
    db.execute("INSERT INTO users VALUES (2, 'bob')")
    rs = db.execute("SELECT * FROM users ORDER BY id")
    print(rs.columns)   # ['id', 'name']
    for row in rs.rows:
        print(row)      # ['1', 'alice']
```

## CLI / REPL

```bash
python -m tinydb example.db
```

Meta-commands:

- `.tables` — list tables
- `.schema <name>` — show a table's CREATE statement
- `.help` — show all meta-commands
- `.exit` / `.quit` — exit the REPL

Type `;` to terminate a SQL statement; multi-line input is fine.

## Supported SQL

- DDL: `CREATE TABLE`, `DROP TABLE`
- DML: `INSERT`, `UPDATE`, `DELETE`, `SELECT`
- Clauses: `WHERE`, `ORDER BY [ASC|DESC]`, `LIMIT`, `OFFSET`, `DISTINCT`, `GROUP BY`
- Aggregates: `COUNT(*)`, `COUNT(col)`, `SUM`, `AVG` (no `MIN`/`MAX`)
- Joins: single-table only (no JOIN in MVP)
- Types: `INT`, `FLOAT`, `TEXT`, `BOOL`
- Constraints: `PRIMARY KEY`, `NOT NULL`, `UNIQUE`
- Transactions: `BEGIN`, `COMMIT`, `ROLLBACK` (single-writer)

## File format

```
[Page 0: FileHeader 4096 bytes]
[Page 1+ : HEAP, BTREE_LEAF, BTREE_INTERNAL, CATALOG, FREE]
```

Each non-header page has a 13-byte in-page header (`page_type`,
`num_slots`, `free_offset`, `next`, `prev`) followed by a payload.
The WAL lives next to the data file as `<dbfile>.wal`.

## Project layout

```
tinydb/
  storage/    # DiskManager, BufferPool, Page, FreeList
  catalog/    # persistent table / column / index metadata
  parser/     # lexer + recursive-descent SQL parser
  types/      # Value, Tag, serialization, type coercion
  executor/   # Volcano operators (Scan, Filter, Join, Aggregate, …)
  index/      # B+ tree
  txn/        # WAL writer + TransactionManager (begin/commit/rollback)
  cli/        # REPL, tabular formatter, meta-commands
tests/
  unit/       # per-module unit tests
  integration # full SQL workflows, CLI tests
openspec/
  changes/init-tinydb/   # proposal, design, specs, tasks
```

## Development

```bash
pytest                    # run all tests
pytest --cov=tinydb       # with coverage (target ≥80%)
```

## License

MIT