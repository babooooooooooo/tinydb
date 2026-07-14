"""AST node definitions for tinydb's SQL subset.

The grammar covered:

    stmt        := create_table | drop_table | insert | select
                 | update | delete | begin | commit | rollback
    create_table := CREATE TABLE ident '(' column_def (',' column_def)* ')'
    column_def  := ident type_name [constraint]*
    type_name   := INT | FLOAT | TEXT | BOOL
    constraint  := NOT NULL | NULL | PRIMARY KEY | UNIQUE
    drop_table  := DROP TABLE ident
    insert      := INSERT INTO ident [ '(' ident (',' ident)* ')' ]
                   VALUES '(' expr (',' expr)* ')'
    select      := SELECT [DISTINCT] select_items FROM ident
                   [WHERE expr] [GROUP BY ident (',' ident)*]
                   [ORDER BY order_item (',' order_item)*]
                   [LIMIT integer] [OFFSET integer]
    update      := UPDATE ident SET assignment (',' assignment) [WHERE expr]
    delete      := DELETE FROM ident [WHERE expr]
    begin       := BEGIN
    commit      := COMMIT
    rollback    := ROLLBACK

    expr        := or_expr
    or_expr     := and_expr (OR and_expr)*
    and_expr    := not_expr (AND not_expr)*
    not_expr    := NOT not_expr | predicate
    predicate   := primary (('=' | '<>' | '<' | '>' | '<=' | '>=') primary)?
    primary     := literal | column_ref | call | '(' expr ')'
    literal     := integer | float | string | bool | NULL
    call        := COUNT '(' ('*' | expr) ')' | SUM '(' expr ')'
                 | AVG '(' expr ')'
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tinydb.types import Tag


# ============================================================================
# Expressions
# ============================================================================


@dataclass(frozen=True)
class Expr:
    """Marker base — concrete expression nodes follow."""


@dataclass(frozen=True)
class Literal(Expr):
    value: Any  # int | float | str | bool | None

    @property
    def tag(self) -> Tag:
        if self.value is None:
            return Tag.NULL
        if isinstance(self.value, bool):
            return Tag.BOOL
        if isinstance(self.value, int):
            return Tag.INT
        if isinstance(self.value, float):
            return Tag.FLOAT
        if isinstance(self.value, str):
            return Tag.TEXT
        raise TypeError(f"unsupported literal type: {type(self.value).__name__}")


@dataclass(frozen=True)
class ColumnRef(Expr):
    name: str
    table: str | None = None  # optional table qualifier


@dataclass(frozen=True)
class Star(Expr):
    """``*`` — used in SELECT items and COUNT(*)."""

    table: str | None = None


@dataclass(frozen=True)
class BinaryOp(Expr):
    op: str  # one of: =, <>, <, >, <=, >=, AND, OR, +, -, *, /
    left: Expr
    right: Expr


@dataclass(frozen=True)
class UnaryOp(Expr):
    op: str  # only NOT for now
    operand: Expr


@dataclass(frozen=True)
class FunctionCall(Expr):
    name: str  # COUNT, SUM, AVG
    arg: Expr  # Star or another Expr


# ============================================================================
# Statements
# ============================================================================


@dataclass(frozen=True)
class Stmt:
    """Marker base — concrete statement nodes follow."""


@dataclass(frozen=True)
class CreateTableStmt(Stmt):
    name: str
    columns: tuple["ColumnDef", ...]


@dataclass(frozen=True)
class ColumnDef:
    name: str
    type: Tag
    not_null: bool = False
    primary_key: bool = False
    unique: bool = False


@dataclass(frozen=True)
class DropTableStmt(Stmt):
    name: str


@dataclass(frozen=True)
class InsertStmt(Stmt):
    table: str
    columns: tuple[str, ...] | None  # None = all columns
    values: tuple[Expr, ...]


@dataclass(frozen=True)
class OrderItem:
    expr: Expr
    desc: bool = False


@dataclass(frozen=True)
class SelectItem:
    expr: Expr
    alias: str | None = None


@dataclass(frozen=True)
class SelectStmt(Stmt):
    distinct: bool = False
    items: tuple[SelectItem, ...] = field(default_factory=tuple)
    from_table: str | None = None
    where: Expr | None = None
    group_by: tuple[str, ...] = field(default_factory=tuple)
    order_by: tuple[OrderItem, ...] = field(default_factory=tuple)
    limit: int | None = None
    offset: int | None = None


@dataclass(frozen=True)
class Assignment:
    column: str
    value: Expr


@dataclass(frozen=True)
class UpdateStmt(Stmt):
    table: str
    assignments: tuple[Assignment, ...]
    where: Expr | None = None


@dataclass(frozen=True)
class DeleteStmt(Stmt):
    table: str
    where: Expr | None = None


@dataclass(frozen=True)
class BeginStmt(Stmt):
    pass


@dataclass(frozen=True)
class CommitStmt(Stmt):
    pass


@dataclass(frozen=True)
class RollbackStmt(Stmt):
    pass