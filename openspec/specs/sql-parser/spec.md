# sql-parser Specification

## Purpose
TBD - created by archiving change init-tinydb. Update Purpose after archive.
## Requirements
### Requirement: Tokenize SQL text
The system SHALL tokenize a SQL string into a stream of tokens (keywords, identifiers, literals, operators, punctuation) and report lexical errors with a 1-based line/column position.

#### Scenario: Lex valid DDL
- **WHEN** the user executes `CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL)`
- **THEN** the lexer returns a token stream beginning with `CREATE`, `TABLE`, identifier `users`, `(`, identifier `id`, `INT`, `PRIMARY`, `KEY`, ...

#### Scenario: Lex integer and string literals
- **WHEN** the user executes `INSERT INTO users VALUES (1, 'alice')`
- **THEN** the lexer produces an integer literal token `1` and a string literal token `'alice'`

#### Scenario: Reject unterminated string
- **WHEN** the user executes `INSERT INTO users VALUES (1, 'alice)`
- **THEN** the parser raises `ParseError` indicating the unterminated string literal

#### Scenario: Keywords are case-insensitive
- **WHEN** the user executes `select * from users`
- **THEN** the lexer treats `select`, `from` as the keywords SELECT and FROM regardless of case

### Requirement: Parse CREATE TABLE
The system SHALL parse `CREATE TABLE <name> (<col_def>[, ...])` into a `CreateTableStmt` AST node containing the table name and an ordered list of column definitions.

#### Scenario: Parse single-column table
- **WHEN** the user executes `CREATE TABLE t (id INT)`
- **THEN** the parser returns a `CreateTableStmt` with name `t` and one column `id` of type `INT`

#### Scenario: Parse column constraints
- **WHEN** the user executes `CREATE TABLE t (id INT PRIMARY KEY, email TEXT UNIQUE NOT NULL)`
- **THEN** the parser returns column `id` with constraints `[PRIMARY_KEY]` and column `email` with constraints `[UNIQUE, NOT NULL]`

#### Scenario: Reject duplicate column names
- **WHEN** the user executes `CREATE TABLE t (id INT, id TEXT)`
- **THEN** the parser raises `ParseError` indicating duplicate column name `id`

### Requirement: Parse INSERT
The system SHALL parse `INSERT INTO <table> [(col, ...)] VALUES (expr, ...)` into an `InsertStmt` AST node.

#### Scenario: Parse insert with all columns
- **WHEN** the user executes `INSERT INTO users VALUES (1, 'alice', true)`
- **THEN** the parser returns an `InsertStmt` referencing table `users` with three literal values and `columns=None`

#### Scenario: Parse insert with column list
- **WHEN** the user executes `INSERT INTO users (name, age) VALUES ('bob', 30)`
- **THEN** the parser returns an `InsertStmt` with `columns=['name', 'age']` and two values

#### Scenario: Reject column/value count mismatch
- **WHEN** the user executes `INSERT INTO users (name) VALUES ('bob', 30)`
- **THEN** the parser raises `ParseError` indicating column/value count mismatch

### Requirement: Parse SELECT
The system SHALL parse `SELECT [DISTINCT] <select_items> FROM <table> [WHERE <expr>] [GROUP BY <col>[, ...]] [ORDER BY <col> [ASC|DESC][, ...]] [LIMIT <n>] [OFFSET <n>]` into a `SelectStmt` AST node.

#### Scenario: Parse select all with where
- **WHEN** the user executes `SELECT * FROM users WHERE age >= 18`
- **THEN** the parser returns a `SelectStmt` with `items=[Star]`, `from=users`, `where=Comparison(age, >=, 18)`

#### Scenario: Parse select with group by and order by
- **WHEN** the user executes `SELECT city, COUNT(*) FROM users GROUP BY city ORDER BY COUNT(*) DESC LIMIT 10`
- **THEN** the parser returns a `SelectStmt` with `group_by=[city]`, `order_by=[(COUNT(*), DESC)]`, `limit=10`

#### Scenario: Parse boolean expression precedence
- **WHEN** the user executes `SELECT * FROM users WHERE age > 18 AND (city = 'BJ' OR city = 'SH')`
- **THEN** the AST represents `AND` with higher precedence than `OR` (correct parenthesization)

### Requirement: Parse UPDATE and DELETE
The system SHALL parse `UPDATE <table> SET <col>=<expr>[, ...] [WHERE <expr>]` and `DELETE FROM <table> [WHERE <expr>]` into corresponding AST nodes.

#### Scenario: Parse update multiple columns
- **WHEN** the user executes `UPDATE users SET age = 31, name = 'alice2' WHERE id = 1`
- **THEN** the parser returns an `UpdateStmt` with two assignments and a `where` clause on `id = 1`

#### Scenario: Parse delete without where
- **WHEN** the user executes `DELETE FROM users`
- **THEN** the parser returns a `DeleteStmt` with `where=None`

### Requirement: Parse DROP TABLE
The system SHALL parse `DROP TABLE <name>` into a `DropTableStmt` AST node.

#### Scenario: Parse drop table
- **WHEN** the user executes `DROP TABLE users`
- **THEN** the parser returns a `DropTableStmt` with name `users`

### Requirement: Parse transaction statements
The system SHALL parse `BEGIN`, `COMMIT`, `ROLLBACK` into `BeginStmt`, `CommitStmt`, `RollbackStmt` AST nodes respectively.

#### Scenario: Parse begin
- **WHEN** the user executes `BEGIN`
- **THEN** the parser returns a `BeginStmt`

#### Scenario: Parse commit
- **WHEN** the user executes `COMMIT`
- **THEN** the parser returns a `CommitStmt`

