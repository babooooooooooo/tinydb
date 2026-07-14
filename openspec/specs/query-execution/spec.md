# query-execution Specification

## Purpose
TBD - created by archiving change init-tinydb. Update Purpose after archive.
## Requirements
### Requirement: Execute CREATE TABLE
The system SHALL create a new table in the catalog and allocate a heap page for it on first INSERT.

#### Scenario: Create new table
- **WHEN** the user executes `CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL)`
- **THEN** a `ResultSet` with `rows_affected=0` is returned and the table is queryable

#### Scenario: Reject duplicate table
- **WHEN** the user executes `CREATE TABLE users (...)` and `users` already exists
- **THEN** the executor raises `ConstraintError` indicating the table already exists

### Requirement: Execute DROP TABLE
The system SHALL remove the table's data pages, indexes, and catalog entry on `DROP TABLE`.

#### Scenario: Drop existing table
- **WHEN** the user executes `DROP TABLE users`
- **THEN** subsequent `SELECT * FROM users` raises `StorageError` indicating the table does not exist

#### Scenario: Drop missing table
- **WHEN** the user executes `DROP TABLE missing`
- **THEN** the executor raises `StorageError` indicating the table does not exist

### Requirement: Execute INSERT
The system SHALL insert one row per VALUES tuple into the target table, enforcing type, NOT NULL, PRIMARY KEY uniqueness, and UNIQUE constraints.

#### Scenario: Successful insert
- **WHEN** the user executes a valid `INSERT INTO users VALUES (...)`
- **THEN** the returned `ResultSet` has `rows_affected=1` and a subsequent `SELECT * FROM users` returns the new row

#### Scenario: Reject duplicate primary key
- **WHEN** the user inserts a row whose PRIMARY KEY already exists
- **THEN** the executor raises `ConstraintError` and the row is NOT inserted

#### Scenario: Reject NULL into NOT NULL column
- **WHEN** the user inserts NULL into a NOT NULL column
- **THEN** the executor raises `ConstraintError`

#### Scenario: Reject value violating UNIQUE
- **WHEN** the user inserts a row whose UNIQUE column value already exists
- **THEN** the executor raises `ConstraintError`

### Requirement: Execute SELECT
The system SHALL evaluate a `SELECT` statement and return a `ResultSet` containing the matching rows and column metadata.

#### Scenario: Select all with no filter
- **WHEN** the user executes `SELECT * FROM users`
- **THEN** the result contains every row in `users` in insertion order

#### Scenario: Select with WHERE
- **WHEN** the user executes `SELECT * FROM users WHERE age >= 18`
- **THEN** the result contains only rows where `age >= 18`

#### Scenario: Select with ORDER BY ASC
- **WHEN** the user executes `SELECT * FROM users ORDER BY age ASC`
- **THEN** rows are returned sorted by `age` ascending

#### Scenario: Select with LIMIT and OFFSET
- **WHEN** the user executes `SELECT * FROM users ORDER BY id LIMIT 2 OFFSET 1`
- **THEN** exactly 2 rows are returned, skipping the first row in sort order

#### Scenario: Select specific columns
- **WHEN** the user executes `SELECT name, age FROM users`
- **THEN** the result contains only `name` and `age` columns

### Requirement: Aggregate functions
The system SHALL support `COUNT(*)`, `COUNT(col)`, `SUM(col)`, `AVG(col)` aggregate functions, optionally with `GROUP BY`.

#### Scenario: COUNT star
- **WHEN** the user executes `SELECT COUNT(*) FROM users`
- **THEN** the result is a single row with the count of all rows

#### Scenario: COUNT column ignores NULL
- **WHEN** the user executes `SELECT COUNT(email) FROM users`
- **THEN** rows where `email IS NULL` are not counted

#### Scenario: GROUP BY
- **WHEN** the user executes `SELECT city, COUNT(*) FROM users GROUP BY city`
- **THEN** the result contains one row per distinct `city`, each with that city's count

#### Scenario: AVG
- **WHEN** the user executes `SELECT AVG(age) FROM users`
- **THEN** the result is the arithmetic mean of non-NULL `age` values

### Requirement: Execute UPDATE
The system SHALL update rows matching `WHERE`, computing new values per assignment. The number of rows affected SHALL be returned.

#### Scenario: Update multiple rows
- **WHEN** the user executes `UPDATE users SET active = false WHERE age < 0`
- **THEN** all matching rows are updated and `rows_affected` equals their count

#### Scenario: Update with no match
- **WHEN** no rows match `WHERE`
- **THEN** `rows_affected` is 0 and no row is modified

#### Scenario: Reject update violating UNIQUE
- **WHEN** an update would create a UNIQUE constraint violation
- **THEN** the executor raises `ConstraintError` and no rows are modified

### Requirement: Execute DELETE
The system SHALL delete rows matching `WHERE` (or all rows when no `WHERE` is given). The number of rows affected SHALL be returned.

#### Scenario: Delete with WHERE
- **WHEN** the user executes `DELETE FROM users WHERE age < 0`
- **THEN** matching rows are removed and `rows_affected` is their count

#### Scenario: Delete all rows
- **WHEN** the user executes `DELETE FROM users`
- **THEN** the table is empty after the statement and `rows_affected` equals the prior row count

### Requirement: Index selection
The system SHALL choose an IndexScan when a WHERE clause's equality or range predicate matches an available index; otherwise it MUST use a SeqScan.

#### Scenario: Equality on indexed column uses index
- **WHEN** the user queries `WHERE id = 5` and `id` has a unique index
- **THEN** the execution plan uses `IndexScan` (verifiable via plan-print or test double)

#### Scenario: Non-indexed predicate uses seq scan
- **WHEN** the user queries `WHERE name = 'x'` and `name` has no index
- **THEN** the execution plan uses `SeqScan`

### Requirement: ResultSet shape
The system SHALL return a `ResultSet` object exposing `.rows` (list of tuples), `.columns` (list of column names), and `.rows_affected` (int, only meaningful for write statements).

#### Scenario: ResultSet for SELECT
- **WHEN** the user executes any `SELECT`
- **THEN** `rs.columns` lists the projected columns and `rs.rows` lists the result tuples in order

