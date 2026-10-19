"""AST node definitions. All nodes carry (line, col) for diagnostics."""
from dataclasses import dataclass, field
from typing import List, Union


@dataclass
class Num:
    value: float
    line: int = 0
    col: int = 0


@dataclass
class Str:
    value: str
    line: int = 0
    col: int = 0


@dataclass
class ColRef:
    name: str
    line: int = 0
    col: int = 0


@dataclass
class BinOp:
    op: str  # + - * /
    left: "Expr"
    right: "Expr"
    line: int = 0
    col: int = 0


Expr = Union[Num, Str, ColRef, BinOp]


@dataclass
class Assign:
    column: str
    expr: Expr
    line: int = 0
    col: int = 0


@dataclass
class Reindex:
    table: str
    columns: List[str]
    line: int = 0
    col: int = 0


@dataclass
class Repartition:
    table: str
    key: str
    line: int = 0
    col: int = 0


@dataclass
class Update:
    table: str
    assigns: List[Assign]
    line: int = 0
    col: int = 0


Statement = Union[Reindex, Repartition, Update]


@dataclass
class Program:
    statements: List[Statement] = field(default_factory=list)
