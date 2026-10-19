"""Lark-based parser: source text -> AST (ast_nodes)."""
from pathlib import Path

from lark import Lark, Transformer, v_args
from lark.exceptions import LarkError, UnexpectedInput

from . import ast_nodes as A
from .errors import ParseError

_GRAMMAR = (Path(__file__).parent / "grammar.lark").read_text(encoding="utf-8")
_LARK = Lark(_GRAMMAR, parser="lalr", propagate_positions=True)


def _pos(meta):
    return getattr(meta, "line", 0), getattr(meta, "column", 0)


def _binop(op):
    def build(self, meta, left, right):
        return A.BinOp(op, left, right, *_pos(meta))
    return build


@v_args(meta=True, inline=True)
class _Builder(Transformer):
    def start(self, meta, *stmts):
        return A.Program(list(stmts))

    def reindex(self, meta, table, *cols):
        return A.Reindex(str(table), [str(c) for c in cols], *_pos(meta))

    def repartition(self, meta, table, key):
        return A.Repartition(str(table), str(key), *_pos(meta))

    def update(self, meta, table, *assigns):
        return A.Update(str(table), list(assigns), *_pos(meta))

    def assign(self, meta, col, expr):
        return A.Assign(str(col), expr, *_pos(meta))

    def number(self, meta, tok):
        return A.Num(float(tok), *_pos(meta))

    def string(self, meta, tok):
        return A.Str(str(tok)[1:-1], *_pos(meta))

    def colref(self, meta, tok):
        return A.ColRef(str(tok), *_pos(meta))

    def neg(self, meta, operand):
        return A.BinOp("-", A.Num(0.0, *_pos(meta)), operand, *_pos(meta))

    add = _binop("+")
    sub = _binop("-")
    mul = _binop("*")
    div = _binop("/")


def parse(source: str) -> A.Program:
    try:
        tree = _LARK.parse(source)
    except UnexpectedInput as e:
        expected = sorted(getattr(e, "expected", None) or [])[:6]
        msg = f"unexpected input, expected one of {expected}" if expected else "unexpected input"
        raise ParseError(msg, getattr(e, "line", None), getattr(e, "column", None)) from None
    except LarkError as e:
        raise ParseError(str(e)) from None
    return _Builder().transform(tree)
