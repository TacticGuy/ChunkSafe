"""Semantic analysis: resolve tables/columns against a schema catalog."""
import sqlite3
from typing import Dict, Set

from . import ast_nodes as A
from .errors import SemanticError


class Catalog:
    """table -> set of column names (case-sensitive)."""

    def __init__(self, tables: Dict[str, Set[str]]):
        self.tables = {t: set(c) for t, c in tables.items()}

    @classmethod
    def from_sqlite(cls, path: str) -> "Catalog":
        con = sqlite3.connect(path)
        try:
            names = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "AND name NOT LIKE '\\_cs\\_%' ESCAPE '\\'")]
            return cls({n: {r[1] for r in con.execute(f'PRAGMA table_info("{n}")')} for n in names})
        finally:
            con.close()


def _expr_cols(e):
    if isinstance(e, A.ColRef):
        yield e
    elif isinstance(e, A.BinOp):
        yield from _expr_cols(e.left)
        yield from _expr_cols(e.right)


def _has_str(e):
    return isinstance(e, A.Str) or (isinstance(e, A.BinOp) and (_has_str(e.left) or _has_str(e.right)))


def check(program: A.Program, catalog: Catalog) -> A.Program:
    for st in program.statements:
        if st.table not in catalog.tables:
            raise SemanticError(f"unknown table '{st.table}'", st.line, st.col)
        cols = catalog.tables[st.table]

        def need(name, line, col):
            if name not in cols:
                raise SemanticError(f"unknown column '{st.table}.{name}'", line, col)

        if isinstance(st, A.Reindex):
            seen = set()
            for c in st.columns:
                need(c, st.line, st.col)
                if c in seen:
                    raise SemanticError(f"duplicate index column '{c}'", st.line, st.col)
                seen.add(c)
        elif isinstance(st, A.Repartition):
            need(st.key, st.line, st.col)
        else:
            targets = set()
            for a in st.assigns:
                need(a.column, a.line, a.col)
                if a.column in targets:
                    raise SemanticError(f"column '{a.column}' assigned twice", a.line, a.col)
                targets.add(a.column)
                for ref in _expr_cols(a.expr):
                    need(ref.name, ref.line, ref.col)
                if isinstance(a.expr, A.BinOp) and _has_str(a.expr):
                    raise SemanticError("string literal in arithmetic expression", a.line, a.col)
    return program
