"""Exception hierarchy for tinydb.

`TypeError` clashes with the builtin; we expose `TypeMismatchError` as the
canonical type-error type and re-export it as `TypeError` at the package
boundary so users can write `except tinydb.TypeError`.
"""

from __future__ import annotations


class TinyDBError(Exception):
    """Base class for all tinydb errors."""


class ParseError(TinyDBError):
    """Raised on lexical or syntactic errors in SQL text."""

    def __init__(self, message: str, line: int = 0, col: int = 0) -> None:
        super().__init__(f"{message} (line {line}, col {col})")
        self.line = line
        self.col = col


class TypeMismatchError(TinyDBError, TypeError):
    """Raised when a value's type does not match the declared column type.

    Inherits from builtin ``TypeError`` so callers that catch stdlib
    ``TypeError`` continue to work, while still being catchable as
    ``tinydb.errors.TinyDBError``.
    """


class ConstraintError(TinyDBError):
    """Raised on constraint violations (NOT NULL, UNIQUE, PK, FK in future)."""


class StorageError(TinyDBError):
    """Raised on disk / page / format errors."""


class TransactionError(TinyDBError):
    """Raised on transaction state violations (nested BEGIN, no active txn, etc.)."""


# Re-export so `tinydb.TypeError` refers to the tinydb-specific error.
# (Builtin ``TypeError`` is still importable from builtins.)
TypeError = TypeMismatchError