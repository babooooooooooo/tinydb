# storage-engine Specification

## Purpose
TBD - created by archiving change init-tinydb. Update Purpose after archive.
## Requirements
### Requirement: Single-file persistence
The system SHALL persist all database state (catalog, table data, indexes) into a single file. The file MUST be usable to reopen the database later via the same path.

#### Scenario: Create then reopen
- **WHEN** the user creates a database at `a.db`, runs `CREATE TABLE t (id INT)`, closes the file, then opens the same path in a new `Database` instance
- **THEN** the new instance shows the table `t` and can `INSERT` / `SELECT` against it

#### Scenario: Open non-existent file creates it
- **WHEN** the user opens a database at a path that does not exist
- **THEN** the system creates an empty database file at that path

### Requirement: Page-based storage
The system SHALL organize on-disk data into fixed-size pages of 4096 bytes. The first page SHALL be a file header containing a magic number, format version, root catalog page id, and free-list head.

#### Scenario: Inspect file header
- **WHEN** the user opens any valid tinydb file
- **THEN** the first 4096 bytes parse into a `FileHeader` with the documented fields populated

#### Scenario: Reject file with wrong magic
- **WHEN** the user opens a file whose first 8 bytes do not match the magic
- **THEN** the system raises `StorageError` indicating invalid file format

### Requirement: Buffer pool with LRU eviction
The system SHALL maintain an in-memory buffer pool of pages. When the pool is full and a new page is requested, the least-recently-used page SHALL be evicted; dirty pages MUST be flushed before eviction.

#### Scenario: Repeated read keeps hot page in pool
- **WHEN** the user repeatedly reads the same table
- **THEN** the page is not re-read from disk on subsequent reads (verifiable via test double on disk layer)

#### Scenario: Dirty page is flushed before eviction
- **WHEN** a dirty page is evicted from the buffer pool
- **THEN** the latest version of that page is persisted to disk before the slot is reused

### Requirement: Free page management
The system SHALL maintain a free-list of page ids available for reuse. Newly allocated pages SHALL come from the free-list first; only when empty SHALL the file grow.

#### Scenario: Reuse freed page
- **WHEN** a page is freed and a new allocation is requested
- **THEN** the freed page id is returned (verifiable by file size not growing)

### Requirement: Catalog persistence
The system SHALL persist the catalog (table definitions, column definitions, indexes) in a dedicated catalog page tree, and reload it on open.

#### Scenario: Catalog survives reopen
- **WHEN** tables `users` and `orders` exist and the database is closed and reopened
- **THEN** both tables are visible and intact

### Requirement: Crash-safe shutdown
The system SHALL flush all dirty pages and the file header to disk on `Database.close()`.

#### Scenario: Clean close leaves valid file
- **WHEN** the user calls `db.close()` after several writes
- **THEN** the file on disk passes a full reopen-and-replay test

