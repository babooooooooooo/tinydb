## ADDED Requirements

### Requirement: REPL entry point
The system SHALL provide a CLI entry point `python -m tinydb <dbfile>` that opens (or creates) the database and starts an interactive REPL.

#### Scenario: Start REPL on existing file
- **WHEN** the user runs `python -m tinydb data.db` where `data.db` is an existing tinydb file
- **THEN** the REPL starts, displays a prompt, and the file is opened read-write

#### Scenario: Start REPL on non-existent file
- **WHEN** the user runs `python -m tinydb new.db` where `new.db` does not exist
- **THEN** a new database file is created at `new.db` and the REPL starts

#### Scenario: Missing argument exits with usage
- **WHEN** the user runs `python -m tinydb` without a path
- **THEN** a usage message is printed and the process exits with non-zero status

### Requirement: Multi-statement input
The REPL SHALL accept statements terminated by `;`. A statement submitted without `;` SHALL be buffered for continuation on the next line.

#### Scenario: Multi-line input
- **WHEN** the user types `INSERT INTO users\nVALUES (1, 'a');`
- **THEN** the REPL buffers the first line, prompts for continuation, and executes the full statement when `;` arrives

#### Scenario: Single-line statement
- **WHEN** the user types `SELECT * FROM users;`
- **THEN** the REPL executes immediately

### Requirement: Result formatting
The REPL SHALL format `SELECT` results as an ASCII table with a header row, column-aligned values, and a row count summary. Write statements SHALL print `<rows_affected> row(s) affected`.

#### Scenario: Tabular SELECT output
- **WHEN** the user executes `SELECT id, name FROM users;`
- **THEN** the output shows a table with header `id | name` and one row per result, plus `N row(s) returned`

#### Scenario: Write statement feedback
- **WHEN** the user executes `INSERT INTO users VALUES (1, 'a');`
- **THEN** the REPL prints `1 row(s) affected`

### Requirement: Meta-commands
The REPL SHALL support meta-commands starting with `.`: at least `.tables`, `.schema <name>`, `.exit` / `.quit`.

#### Scenario: List tables
- **WHEN** the user types `.tables`
- **THEN** the REPL prints a list of all table names in the database

#### Scenario: Show schema
- **WHEN** the user types `.schema users`
- **THEN** the REPL prints the `CREATE TABLE` statement that defines `users`

#### Scenario: Exit REPL
- **WHEN** the user types `.exit` or `.quit`
- **THEN** the REPL closes the database cleanly and exits with status 0

### Requirement: Error display
The REPL SHALL catch all `tinydb.errors.TinyDBError` (and subclasses) and display a single user-friendly line; the REPL SHALL NOT crash on any user-input error.

#### Scenario: Parse error in REPL
- **WHEN** the user types `SELEC * FROM users;`
- **THEN** the REPL prints `ParseError: ...` and returns to the prompt without exiting

#### Scenario: Constraint violation in REPL
- **WHEN** the user types an INSERT that violates a UNIQUE constraint
- **THEN** the REPL prints `ConstraintError: ...` and returns to the prompt