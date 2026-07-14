## ADDED Requirements

### Requirement: Supported value types
The system SHALL support exactly four scalar value types: `INT`, `FLOAT`, `TEXT`, `BOOL`, plus a `NULL` marker. No other value types are accepted in v1.

#### Scenario: Reject unsupported type in CREATE TABLE
- **WHEN** the user executes `CREATE TABLE t (data BLOB)`
- **THEN** the parser raises `ParseError` indicating unsupported type `BLOB`

### Requirement: Type validation on write
The system SHALL validate that every value written to a column matches the column's declared type; mismatch MUST raise `TypeError`.

#### Scenario: Insert int into text column
- **WHEN** the user executes `INSERT INTO users (name) VALUES (123)` against `name TEXT`
- **THEN** the executor raises `TypeError` with a message naming column `name` and expected type `TEXT`

#### Scenario: Insert string into int column
- **WHEN** the user executes `INSERT INTO users (age) VALUES ('thirty')` against `age INT`
- **THEN** the executor raises `TypeError` with a message naming column `age` and expected type `INT`

#### Scenario: Accept numeric literal into float column
- **WHEN** the user executes `INSERT INTO t (price) VALUES (9.99)` against `price FLOAT`
- **THEN** the insert succeeds

#### Scenario: Accept bool into bool column
- **WHEN** the user executes `INSERT INTO t (active) VALUES (true)` against `active BOOL`
- **THEN** the insert succeeds

### Requirement: NULL handling
The system SHALL represent SQL NULL as a distinct value. NULLs SHALL NOT match equality with anything (including NULL); comparison with NULL yields NULL (treated as false in WHERE).

#### Scenario: NULL fails equality
- **WHEN** the user executes `SELECT * FROM users WHERE name = NULL`
- **THEN** the result is empty (no rows match)

#### Scenario: NULL inserted into NOT NULL column rejected
- **WHEN** the user executes `INSERT INTO users (name) VALUES (NULL)` against `name TEXT NOT NULL`
- **THEN** the executor raises `ConstraintError`

### Requirement: Type-aware comparison
The system SHALL compare values using SQL-like semantics: INT < FLOAT promotion is permitted in comparisons; TEXT compares lexicographically by codepoint; BOOL compares as false < true.

#### Scenario: Compare int and float
- **WHEN** the user executes `SELECT * FROM t WHERE x > 1` against `x FLOAT`
- **THEN** an INT literal `1` is treated as `1.0` for the comparison

#### Scenario: Compare texts
- **WHEN** the user executes `SELECT * FROM t WHERE name > 'bob'`
- **THEN** rows where `name` is lexicographically greater than `'bob'` (by codepoint) are returned

### Requirement: Type serialization round-trip
The system SHALL serialize each value to bytes and be able to deserialize back to the original Python value without loss for INT/FLOAT/TEXT/BOOL/NULL.

#### Scenario: Round-trip int
- **WHEN** a value `INT 42` is serialized then deserialized
- **THEN** the deserialized value equals `42` and has type `INT`

#### Scenario: Round-trip text with multi-byte characters
- **WHEN** a value `TEXT '你好'` is serialized then deserialized
- **THEN** the deserialized value equals `'你好'`

#### Scenario: Round-trip NULL
- **WHEN** a `NULL` value is serialized then deserialized
- **THEN** the deserialized value is `NULL`