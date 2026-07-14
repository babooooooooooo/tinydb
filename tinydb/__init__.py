"""tinydb - a lightweight embedded relational database for Python.

Public API:
    Database: open or create a database file and execute SQL.
    ResultSet: rows + columns + rows_affected returned by execute().
    TypeError: alias for ``tinydb.errors.TypeMismatchError`` (distinct from
        builtin ``TypeError`` so callers can disambiguate).
"""

from __future__ import annotations

from tinydb.errors import TypeMismatchError

__all__ = ["Database", "ResultSet", "TypeError"]
__version__ = "0.1.0"

# Re-export the type-error class under the package namespace.
# Note: this shadows builtin ``TypeError`` for ``tinydb.TypeError`` lookups
# only; the builtin remains importable from builtins.
TypeError = TypeMismatchError


def __getattr__(name: str):
    # Lazy resolution for Database/ResultSet so importing tinydb doesn't
    # require those classes to be implemented yet (the subsystems land in
    # later phases).
    if name in ("Database", "ResultSet"):
        from tinydb import database as _database

        return getattr(_database, name)
    raise AttributeError(f"module 'tinydb' has no attribute {name!r}")