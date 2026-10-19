"""Lexing is provided by Lark's contextual lexer (see grammar.lark).

This module exposes a small debugging helper and the keyword set.
"""
from .parser import _LARK

KEYWORDS = {"REINDEX", "ON", "REPARTITION", "BY", "UPDATE", "SET"}


def tokenize(source: str):
    """Return [(type, text, line, col), ...] for debugging."""
    return [(t.type, str(t), t.line, t.column) for t in _LARK.lex(source)]
