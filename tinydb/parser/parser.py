"""Recursive-descent parser for tinydb's SQL subset.

Grammar and precedence:

    expr        := or_expr
    or_expr     := and_expr (OR and_expr)*
    and_expr    := unary (AND unary)*
    unary       := NOT unary | predicate
    predicate   := add_expr (comp_op add_expr)?
    comp_op     := '=' | '<>' | '<' | '>' | '<=' | '>='
    add_expr    := mul_expr (('+' | '-') mul_expr)*
    mul_expr    := primary (('*' | '/') primary)*
    primary     := literal | column_ref | call | '(' expr ')' | '-' primary

Each top-level call returns one ``Stmt``. ``parse(sql)`` accepts multiple
``;``-separated statements.
"""

from __future__ import annotations

from tinydb.errors import ParseError
from tinydb.parser.ast import (
    Assignment,
    BeginStmt,
    BinaryOp,
    ColumnDef,
    ColumnRef,
    CommitStmt,
    CreateTableStmt,
    DeleteStmt,
    DropTableStmt,
    Expr,
    FunctionCall,
    InsertStmt,
    Literal,
    OrderItem,
    RollbackStmt,
    SelectItem,
    SelectStmt,
    Star,
    Stmt,
    UnaryOp,
    UpdateStmt,
)
from tinydb.parser.lexer import KEYWORDS, Lexer, TokKind, Token
from tinydb.types import Tag


# Operators allowed at the comparison level.
_COMPARISON_OPS: frozenset[str] = frozenset({"=", "<>", "<", ">", "<=", ">="})


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.i = 0

    # ---- public -----------------------------------------------------------

    def parse_program(self) -> list[Stmt]:
        stmts: list[Stmt] = []
        while not self._at(TokKind.EOF):
            stmt = self._parse_statement()
            if stmt is not None:
                stmts.append(stmt)
            # Optional trailing semicolon before EOF.
            if self._at(TokKind.SEMI):
                self._advance()
        return stmts

    # ---- statements -------------------------------------------------------

    def _parse_statement(self) -> Stmt:
        t = self._peek()
        if t.kind is TokKind.KEYWORD:
            kw = t.lexeme
            if kw == "CREATE":
                return self._parse_create_table()
            if kw == "DROP":
                return self._parse_drop_table()
            if kw == "INSERT":
                return self._parse_insert()
            if kw == "SELECT":
                return self._parse_select()
            if kw == "UPDATE":
                return self._parse_update()
            if kw == "DELETE":
                return self._parse_delete()
            if kw == "BEGIN":
                self._advance(); return BeginStmt()
            if kw == "COMMIT":
                self._advance(); return CommitStmt()
            if kw == "ROLLBACK":
                self._advance(); return RollbackStmt()
        raise self._err(f"expected a SQL statement, got {t.lexeme!r}")

    def _parse_create_table(self) -> CreateTableStmt:
        self._expect_keyword("CREATE")
        self._expect_keyword("TABLE")
        name_tok = self._expect(TokKind.IDENT)
        self._expect(TokKind.LPAREN)
        cols: list[ColumnDef] = []
        if not self._at(TokKind.RPAREN):
            cols.append(self._parse_column_def())
            while self._at(TokKind.COMMA):
                self._advance()
                cols.append(self._parse_column_def())
        self._expect(TokKind.RPAREN)
        # Check duplicate column names early (catalog does too).
        seen: set[str] = set()
        for c in cols:
            if c.name in seen:
                raise ParseError(f"duplicate column {c.name!r}")
            seen.add(c.name)
        return CreateTableStmt(name=name_tok.lexeme, columns=tuple(cols))

    def _parse_column_def(self) -> ColumnDef:
        name_tok = self._expect(TokKind.IDENT)
        type_tok = self._peek()
        if type_tok.kind is TokKind.KEYWORD and type_tok.lexeme in ("DOUBLE", "REAL"):
            self._advance()
            col_type = Tag.FLOAT
        elif type_tok.kind is TokKind.KEYWORD and type_tok.lexeme == "BOOLEAN":
            self._advance()
            col_type = Tag.BOOL
        elif (
            type_tok.kind is TokKind.KEYWORD
            and type_tok.lexeme in ("INT", "FLOAT", "TEXT", "BOOL")
        ):
            self._advance()
            col_type = Tag[type_tok.lexeme]
        elif type_tok.kind is TokKind.KEYWORD and type_tok.lexeme in (
            "VARCHAR",
            "CHAR",
            "DECIMAL",
            "DATE",
            "TIME",
            "TIMESTAMP",
            "SMALLINT",
            "BIGINT",
        ):
            self._advance()
            col_type = Tag[type_tok.lexeme]
        else:
            raise self._err(f"expected a column type, got {type_tok.lexeme!r}")
        # Optional parenthesized parameter list: VARCHAR(N), CHAR(N),
        # DECIMAL(p, s). Only meaningful for parameterized types; ignored
        # for zero-arg types like DATE / SMALLINT (still parses harmlessly).
        params: tuple[int, ...] = ()
        if self._at(TokKind.LPAREN):
            self._advance()
            params_list: list[int] = []
            while True:
                tok = self._expect(TokKind.INT)
                params_list.append(int(tok.lexeme))
                if self._at(TokKind.COMMA):
                    self._advance()
                    continue
                if self._at(TokKind.RPAREN):
                    self._advance()
                    break
                raise self._err("expected ',' or ')' in type parameter list")
            params = tuple(params_list)
        not_null = False
        primary_key = False
        unique = False
        while True:
            t = self._peek()
            if t.kind is TokKind.KEYWORD and t.lexeme == "NOT":
                self._advance()
                self._expect(TokKind.NULL)
                not_null = True
            elif t.kind is TokKind.KEYWORD and t.lexeme == "PRIMARY":
                self._advance()
                self._expect_keyword("KEY")
                primary_key = True
            elif t.kind is TokKind.KEYWORD and t.lexeme == "UNIQUE":
                self._advance()
                unique = True
            elif t.kind is TokKind.NULL:
                self._advance()  # explicit NULL: no constraint
            else:
                break
        return ColumnDef(
            name=name_tok.lexeme,
            type=col_type,
            not_null=not_null,
            primary_key=primary_key,
            unique=unique,
            params=params,
        )

    def _parse_drop_table(self) -> DropTableStmt:
        self._expect_keyword("DROP")
        self._expect_keyword("TABLE")
        name = self._expect(TokKind.IDENT)
        return DropTableStmt(name=name.lexeme)

    def _parse_insert(self) -> InsertStmt:
        self._expect_keyword("INSERT")
        self._expect_keyword("INTO")
        name = self._expect(TokKind.IDENT)
        columns: tuple[str, ...] | None = None
        if self._at(TokKind.LPAREN):
            self._advance()
            cols = [self._expect(TokKind.IDENT).lexeme]
            while self._at(TokKind.COMMA):
                self._advance()
                cols.append(self._expect(TokKind.IDENT).lexeme)
            self._expect(TokKind.RPAREN)
            columns = tuple(cols)
        self._expect_keyword("VALUES")
        self._expect(TokKind.LPAREN)
        values = [self._parse_expr()]
        while self._at(TokKind.COMMA):
            self._advance()
            values.append(self._parse_expr())
        self._expect(TokKind.RPAREN)
        return InsertStmt(table=name.lexeme, columns=columns, values=tuple(values))

    def _parse_select(self) -> SelectStmt:
        self._expect_keyword("SELECT")
        distinct = False
        if self._at(TokKind.KEYWORD) and self._peek().lexeme == "DISTINCT":
            distinct = True
            self._advance()
        items = [self._parse_select_item()]
        while self._at(TokKind.COMMA):
            self._advance()
            items.append(self._parse_select_item())
        if not self._at(TokKind.KEYWORD) or self._peek().lexeme != "FROM":
            raise self._err("expected FROM")
        self._advance()
        table = self._expect(TokKind.IDENT).lexeme
        # Optional table alias (e.g. ``FROM users u``).
        if self._at(TokKind.IDENT):
            self._advance()
        where = None
        if self._at(TokKind.KEYWORD) and self._peek().lexeme == "WHERE":
            self._advance()
            where = self._parse_expr()
        group_by: tuple[str, ...] = ()
        if self._at(TokKind.KEYWORD) and self._peek().lexeme == "GROUP":
            self._advance()
            self._expect_keyword("BY")
            gcols = [self._expect(TokKind.IDENT).lexeme]
            while self._at(TokKind.COMMA):
                self._advance()
                gcols.append(self._expect(TokKind.IDENT).lexeme)
            group_by = tuple(gcols)
        order_by: tuple[OrderItem, ...] = ()
        if self._at(TokKind.KEYWORD) and self._peek().lexeme == "ORDER":
            self._advance()
            self._expect_keyword("BY")
            ob = [self._parse_order_item()]
            while self._at(TokKind.COMMA):
                self._advance()
                ob.append(self._parse_order_item())
            order_by = tuple(ob)
        limit: int | None = None
        offset: int | None = None
        if self._at(TokKind.KEYWORD) and self._peek().lexeme == "LIMIT":
            self._advance()
            limit = self._parse_int_literal()
        if self._at(TokKind.KEYWORD) and self._peek().lexeme == "OFFSET":
            self._advance()
            offset = self._parse_int_literal()
        return SelectStmt(
            distinct=distinct,
            items=tuple(items),
            from_table=table,
            where=where,
            group_by=group_by,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )

    def _parse_select_item(self) -> SelectItem:
        if self._at(TokKind.STAR):
            self._advance()
            return SelectItem(expr=Star())
        expr = self._parse_expr()
        alias = None
        if self._at(TokKind.KEYWORD) and self._peek().lexeme == "AS":
            self._advance()
            alias = self._expect(TokKind.IDENT).lexeme
        return SelectItem(expr=expr, alias=alias)

    def _parse_order_item(self) -> OrderItem:
        expr = self._parse_expr()
        desc = False
        if self._at(TokKind.KEYWORD) and self._peek().lexeme == "DESC":
            self._advance()
            desc = True
        elif self._at(TokKind.KEYWORD) and self._peek().lexeme == "ASC":
            self._advance()
        return OrderItem(expr=expr, desc=desc)

    def _parse_update(self) -> UpdateStmt:
        self._expect_keyword("UPDATE")
        name = self._expect(TokKind.IDENT)
        self._expect_keyword("SET")
        assigns = [self._parse_assignment()]
        while self._at(TokKind.COMMA):
            self._advance()
            assigns.append(self._parse_assignment())
        where = None
        if self._at(TokKind.KEYWORD) and self._peek().lexeme == "WHERE":
            self._advance()
            where = self._parse_expr()
        return UpdateStmt(
            table=name.lexeme,
            assignments=tuple(assigns),
            where=where,
        )

    def _parse_assignment(self) -> Assignment:
        col = self._expect(TokKind.IDENT)
        self._expect_op("=")
        val = self._parse_expr()
        return Assignment(column=col.lexeme, value=val)

    def _parse_delete(self) -> DeleteStmt:
        self._expect_keyword("DELETE")
        self._expect_keyword("FROM")
        name = self._expect(TokKind.IDENT)
        where = None
        if self._at(TokKind.KEYWORD) and self._peek().lexeme == "WHERE":
            self._advance()
            where = self._parse_expr()
        return DeleteStmt(table=name.lexeme, where=where)

    # ---- expressions ------------------------------------------------------

    def _parse_expr(self) -> Expr:
        return self._parse_or()

    def _parse_or(self) -> Expr:
        left = self._parse_and()
        while self._at(TokKind.KEYWORD) and self._peek().lexeme == "OR":
            self._advance()
            right = self._parse_and()
            left = BinaryOp(op="OR", left=left, right=right)
        return left

    def _parse_and(self) -> Expr:
        left = self._parse_unary()
        while self._at(TokKind.KEYWORD) and self._peek().lexeme == "AND":
            self._advance()
            right = self._parse_unary()
            left = BinaryOp(op="AND", left=left, right=right)
        return left

    def _parse_unary(self) -> Expr:
        if self._at(TokKind.KEYWORD) and self._peek().lexeme == "NOT":
            self._advance()
            return UnaryOp(op="NOT", operand=self._parse_unary())
        return self._parse_predicate()

    def _parse_predicate(self) -> Expr:
        left = self._parse_add()
        t = self._peek()
        if t.kind is TokKind.OP and t.lexeme in _COMPARISON_OPS:
            op = t.lexeme
            self._advance()
            right = self._parse_add()
            return BinaryOp(op=op, left=left, right=right)
        return left

    def _parse_add(self) -> Expr:
        left = self._parse_mul()
        while self._at(TokKind.OP) and self._peek().lexeme in ("+", "-"):
            op = self._peek().lexeme
            self._advance()
            right = self._parse_mul()
            left = BinaryOp(op=op, left=left, right=right)
        return left

    def _parse_mul(self) -> Expr:
        left = self._parse_unary_sign()
        while self._at(TokKind.OP) and self._peek().lexeme in ("*", "/"):
            op = self._peek().lexeme
            self._advance()
            right = self._parse_unary_sign()
            left = BinaryOp(op=op, left=left, right=right)
        return left

    def _parse_unary_sign(self) -> Expr:
        if self._at(TokKind.OP) and self._peek().lexeme == "-":
            self._advance()
            inner = self._parse_unary_sign()
            # Encode as 0 - inner.
            return BinaryOp(op="-", left=Literal(0), right=inner)
        return self._parse_primary()

    def _parse_primary(self) -> Expr:
        t = self._peek()
        if t.kind is TokKind.INT:
            self._advance()
            return Literal(int(t.lexeme))
        if t.kind is TokKind.FLOAT:
            self._advance()
            return Literal(float(t.lexeme))
        if t.kind is TokKind.STRING:
            self._advance()
            return Literal(t.lexeme)
        if t.kind is TokKind.BOOL:
            self._advance()
            return Literal(t.lexeme == "TRUE")
        if t.kind is TokKind.NULL:
            self._advance()
            return Literal(None)
        if t.kind is TokKind.LPAREN:
            self._advance()
            inner = self._parse_expr()
            self._expect(TokKind.RPAREN)
            return inner
        if t.kind is TokKind.STAR:
            self._advance()
            return Star()
        if t.kind in (TokKind.IDENT, TokKind.KEYWORD):
            name = t.lexeme
            self._advance()
            # Function call?
            if self._at(TokKind.LPAREN):
                self._advance()
                if self._at(TokKind.STAR):
                    self._advance()
                    arg: Expr = Star()
                else:
                    arg = self._parse_expr()
                self._expect(TokKind.RPAREN)
                upper = name.upper()
                if upper not in ("COUNT", "SUM", "AVG"):
                    raise self._err(f"unknown function {name!r}")
                return FunctionCall(name=upper, arg=arg)
            # Otherwise column reference (possibly qualified).
            if self._at(TokKind.OP) and self._peek().lexeme == ".":
                self._advance()
                col = self._expect(TokKind.IDENT).lexeme
                return ColumnRef(name=col, table=name)
            return ColumnRef(name=name)
        raise self._err(f"unexpected token {t.lexeme!r}")

    def _parse_int_literal(self) -> int:
        t = self._expect(TokKind.INT)
        return int(t.lexeme)

    # ---- helpers ----------------------------------------------------------

    def _peek(self, offset: int = 0) -> Token:
        return self.tokens[self.i + offset]

    def _at(self, kind: TokKind, lexeme: str | None = None) -> bool:
        t = self.tokens[self.i]
        if t.kind is not kind:
            return False
        if lexeme is not None and t.lexeme != lexeme:
            return False
        return True

    def _advance(self) -> Token:
        t = self.tokens[self.i]
        self.i += 1
        return t

    def _expect(self, kind: TokKind) -> Token:
        t = self._peek()
        if t.kind is not kind:
            if kind is TokKind.IDENT and t.kind is TokKind.KEYWORD and t.lexeme in KEYWORDS:
                raise self._err(
                    f"{t.lexeme!r} is a reserved keyword; rename it or quote it"
                )
            raise self._err(f"expected {kind.name.lower()}, got {t.lexeme!r}")
        return self._advance()

    def _expect_keyword(self, kw: str) -> Token:
        t = self._peek()
        if t.kind is not TokKind.KEYWORD or t.lexeme != kw:
            raise self._err(f"expected keyword {kw}, got {t.lexeme!r}")
        return self._advance()

    def _expect_op(self, op: str) -> Token:
        t = self._peek()
        if t.kind is not TokKind.OP or t.lexeme != op:
            raise self._err(f"expected operator {op}, got {t.lexeme!r}")
        return self._advance()

    def _err(self, msg: str) -> ParseError:
        t = self._peek()
        return ParseError(msg, t.line, t.col)


def parse(sql: str) -> list[Stmt]:
    """Parse one or more ``;``-separated statements from ``sql``."""
    tokens = Lexer(sql).tokenize()
    return Parser(tokens).parse_program()