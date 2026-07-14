## ADDED Requirements

### Requirement: BEGIN / COMMIT / ROLLBACK
The system SHALL support the statements `BEGIN`, `COMMIT`, `ROLLBACK` to control transactions. Outside an explicit transaction block, each statement SHALL auto-commit.

#### Scenario: Explicit commit
- **WHEN** the user runs `BEGIN; INSERT INTO t VALUES (1); COMMIT;`
- **THEN** after the COMMIT, the change survives a process restart

#### Scenario: Explicit rollback
- **WHEN** the user runs `BEGIN; INSERT INTO t VALUES (1); ROLLBACK;`
- **THEN** after the ROLLBACK, the change is NOT visible in any subsequent `SELECT`

#### Scenario: Auto-commit by default
- **WHEN** the user runs `INSERT INTO t VALUES (1)` without `BEGIN`
- **THEN** the change is durable immediately on statement completion

### Requirement: Atomicity
The system SHALL ensure that either ALL operations in a transaction are persisted or NONE are.

#### Scenario: Multi-statement transaction atomicity
- **WHEN** the user runs `BEGIN; INSERT INTO a VALUES (1); INSERT INTO b VALUES (2); COMMIT;`
- **THEN** a crash after the first INSERT but before COMMIT leaves neither row visible after recovery

### Requirement: Write-Ahead Log (WAL)
The system SHALL use a Write-Ahead Log to record all data page modifications before they reach the data file. The WAL file SHALL be named `<dbfile>.wal`.

#### Scenario: Log records precede data flush
- **WHEN** a write transaction modifies a page
- **THEN** the corresponding WAL record is fsynced before the modified data page is written to the data file

#### Scenario: Recovery from WAL on open
- **WHEN** the user opens a database after a non-clean shutdown
- **THEN** the system replays committed WAL records and rolls back uncommitted ones

### Requirement: Single-writer serialization
The system SHALL serialize write transactions within a single process; concurrent writers SHALL be rejected with `TransactionError`.

#### Scenario: Nested BEGIN rejected
- **WHEN** the user runs `BEGIN; BEGIN;`
- **THEN** the inner `BEGIN` raises `TransactionError` indicating an active transaction already exists

#### Scenario: Statement outside transaction while one is active
- **WHEN** the user runs `BEGIN; INSERT INTO t VALUES (1); SELECT * FROM t;`
- **THEN** the SELECT runs inside the same transaction (auto-commit does not trigger)

### Requirement: Checkpoint
The system SHALL provide a checkpoint operation that flushes all dirty pages and truncates the WAL.

#### Scenario: Checkpoint after many writes
- **WHEN** the user runs `CHECKPOINT;` (or its equivalent) after many committed writes
- **THEN** the WAL file is truncated/empty and the data file reflects all changes

### Requirement: Isolation from uncommitted writes
No other reader (in a separate transaction) SHALL observe writes made by an in-progress transaction.

#### Scenario: Reader sees pre-image
- **WHEN** transaction A has uncommitted writes and transaction B reads the same rows
- **THEN** B observes the values as of the last commit, not A's pending writes