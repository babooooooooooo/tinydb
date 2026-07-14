"""Lexer for tinydb's SQL subset.

Produces a stream of ``Token`` objects. Whitespace and ``--`` line comments
are skipped. Raises ``ParseError`` on unknown characters or unterminated
string literals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto

from tinydb.errors import ParseError


class TokKind(Enum):
    KEYWORD = auto()
    IDENT = auto()
    INT = auto()
    FLOAT = auto()
    STRING = auto()
    BOOL = auto()
    NULL = auto()
    OP = auto()
    LPAREN = auto()
    RPAREN = auto()
    COMMA = auto()
    SEMI = auto()
    STAR = auto()
    EOF = auto()


KEYWORDS: frozenset[str] = frozenset(
    {
        "CREATE",
        "TABLE",
        "DROP",
        "INSERT",
        "INTO",
        "VALUES",
        "SELECT",
        "FROM",
        "WHERE",
        "AND",
        "OR",
        "NOT",
        "NULL",
        "TRUE",
        "FALSE",
        "DISTINCT",
        "GROUP",
        "BY",
        "ORDER",
        "ASC",
        "DESC",
        "LIMIT",
        "OFFSET",
        "UPDATE",
        "SET",
        "DELETE",
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "INT",
        "FLOAT",
        "TEXT",
        "BOOL",
        "PRIMARY",
        "KEY",
        "UNIQUE",
        "AS",
        "COUNT",
        "SUM",
        "AVG",
        "DOUBLE",
        "REAL",
        "BOOLEAN",
    }
)


@dataclass(frozen=True)
class Token:
    kind: TokKind
    lexeme: str
    line: int
    col: int


# Token patterns (longest match first; STRING captures the body without quotes).
_INT_RE = re.compile(r"\d+")
_FLOAT_RE = re.compile(
    r"\d+\.\d*([eE][+-]?\d+)?"   # 1.5, 1.5e10, 1., 1.e10
    r"|\.\d+([eE][+-]?\d+)?"     # .5, .5e10
    r"|\d+[eE][+-]?\d+"          # 1e5, 1e+5, 1e-5
)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STRING_RE = re.compile(r"'((?:[^']|'')*)'")  # doubled '' is an escaped quote


class Lexer:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.line = 1
        self.col = 1

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            # Whitespace.
            if ch in " \t\r\n":
                self._advance(ch)
                continue
            # Line comment: -- ... \n
            if ch == "-" and self._peek(1) == "-":
                while self.pos < len(self.text) and self.text[self.pos] != "\n":
                    self._advance(self.text[self.pos])
                continue
            line, col = self.line, self.col

            # String literal.
            if ch == "'":
                m = _STRING_RE.match(self.text, self.pos)
                if m is None:
                    raise ParseError("unterminated string literal", line, col)
                body = m.group(1).replace("''", "'")
                self._advance_cols(m.end() - m.start())
                tokens.append(Token(TokKind.STRING, body, line, col))
                continue

            # Float (must come before INT).
            m = _FLOAT_RE.match(self.text, self.pos)
            if m is not None and (m.end() == len(self.text) or self.text[m.end()] not in _IDENT_CHARS):
                self._advance_cols(m.end() - m.start())
                tokens.append(Token(TokKind.FLOAT, m.group(0), line, col))
                continue

            # Integer.
            m = _INT_RE.match(self.text, self.pos)
            if m is not None and (m.end() == len(self.text) or self.text[m.end()] not in _IDENT_CHARS):
                self._advance_cols(m.end() - m.start())
                tokens.append(Token(TokKind.INT, m.group(0), line, col))
                continue

            # Identifier / keyword.
            m = _IDENT_RE.match(self.text, self.pos)
            if m is not None:
                word = m.group(0)
                self._advance_cols(m.end() - m.start())
                upper = word.upper()
                if upper in KEYWORDS:
                    if upper in ("TRUE", "FALSE"):
                        tokens.append(Token(TokKind.BOOL, upper, line, col))
                    elif upper == "NULL":
                        tokens.append(Token(TokKind.NULL, "NULL", line, col))
                    else:
                        tokens.append(Token(TokKind.KEYWORD, upper, line, col))
                else:
                    tokens.append(Token(TokKind.IDENT, word, line, col))
                continue

            # Single- and two-character operators/punctuation.
            if ch == "(":
                self._advance(ch); tokens.append(Token(TokKind.LPAREN, "(", line, col)); continue
            if ch == ")":
                self._advance(ch); tokens.append(Token(TokKind.RPAREN, ")", line, col)); continue
            if ch == ",":
                self._advance(ch); tokens.append(Token(TokKind.COMMA, ",", line, col)); continue
            if ch == ";":
                self._advance(ch); tokens.append(Token(TokKind.SEMI, ";", line, col)); continue
            if ch == "*":
                self._advance(ch); tokens.append(Token(TokKind.STAR, "*", line, col)); continue

            # Two-character ops.
            two = self.text[self.pos : self.pos + 2]
            if two in ("<=", ">=", "<>", "!="):
                self._advance(ch); self._advance(self.text[self.pos])
                tokens.append(Token(TokKind.OP, two, line, col))
                continue

            # Single-character ops.
            if ch in "+-*/<>=!":
                self._advance(ch)
                tokens.append(Token(TokKind.OP, ch, line, col))
                continue

            if ch == ".":
                # Used only for qualified column refs (table.column).
                self._advance(ch)
                tokens.append(Token(TokKind.OP, ".", line, col))
                continue

            raise ParseError(f"unexpected character {ch!r}", line, col)

        tokens.append(Token(TokKind.EOF, "", self.line, self.col))
        return tokens

    # ---- internals --------------------------------------------------------

    def _advance(self, ch: str) -> None:
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1

    def _advance_cols(self, n: int) -> None:
        for i in range(n):
            ch = self.text[self.pos]
            self.pos += 1
            if ch == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1

    def _peek(self, offset: int) -> str:
        idx = self.pos + offset
        return self.text[idx] if idx < len(self.text) else ""


_IDENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")