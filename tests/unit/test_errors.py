"""Tests for the exception hierarchy."""

from __future__ import annotations

import pytest

import tinydb
from tinydb.errors import (
    ConstraintError,
    ParseError,
    StorageError,
    TinyDBError,
    TransactionError,
    TypeMismatchError,
)


def test_tinydb_error_is_base() -> None:
    for cls in (ParseError, ConstraintError, TypeMismatchError, StorageError, TransactionError):
        assert issubclass(cls, TinyDBError)


def test_type_mismatch_error_is_builtin_type_error() -> None:
    # Inherits from builtin TypeError so callers catching stdlib TypeError still work.
    assert issubclass(TypeMismatchError, TypeError)


def test_tinydb_type_error_alias() -> None:
    # tinydb.TypeError should resolve to TypeMismatchError.
    assert tinydb.TypeError is TypeMismatchError


def test_parse_error_includes_position() -> None:
    err = ParseError("unexpected token", line=3, col=7)
    msg = str(err)
    assert "line 3" in msg
    assert "col 7" in msg
    assert "unexpected token" in msg


def test_can_catch_all_via_base() -> None:
    with pytest.raises(TinyDBError):
        raise ConstraintError("dup pk")


def test_can_catch_type_mismatch_via_builtin_type_error() -> None:
    with pytest.raises(TypeError):
        raise TypeMismatchError("wrong type")