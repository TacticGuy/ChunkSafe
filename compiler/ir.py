"""Intermediate representation (design.md section 4) and AST -> IR lowering."""
from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, List, Tuple, Union

from . import ast_nodes as A


class Kind(str, Enum):
    REINDEX = "REINDEX"
    REPARTITION = "REPARTITION"
    UPDATE = "UPDATE"


@dataclass(frozen=True)
class ReindexSpec:
    index_cols: Tuple[str, ...]


@dataclass(frozen=True)
class RepartitionSpec:
    key: str


@dataclass(frozen=True)
class UpdateSpec:
    assignments: Tuple[Tuple[str, A.Expr], ...]


@dataclass
class Chunk:
    idx: int
    lo: int  # inclusive
    hi: int  # exclusive


@dataclass
class ChunkPlan:
    chunk_rows: int
    t_opt: float
    est_chunks: int


@dataclass
class Task:
    id: int
    kind: Kind
    table: str
    reads: FrozenSet[str]
    writes: FrozenSet[str]
    spec: Union[ReindexSpec, RepartitionSpec, UpdateSpec]
    line: int = 0
    col: int = 0
    deps: List[int] = field(default_factory=list)   # filled by safety pass
    plan: ChunkPlan = None                          # filled by sizing pass
    chunks: List[Chunk] = field(default_factory=list)


def _cols(e) -> set:
    if isinstance(e, A.ColRef):
        return {e.name}
    if isinstance(e, A.BinOp):
        return _cols(e.left) | _cols(e.right)
    return set()


def lower(program: A.Program) -> List[Task]:
    """Linear IR; reads/writes derived mechanically from the spec."""
    tasks = []
    for i, st in enumerate(program.statements, 1):
        if isinstance(st, A.Reindex):
            t = Task(i, Kind.REINDEX, st.table, frozenset(st.columns), frozenset(),
                     ReindexSpec(tuple(st.columns)), st.line, st.col)
        elif isinstance(st, A.Repartition):
            t = Task(i, Kind.REPARTITION, st.table, frozenset({st.key}), frozenset(),
                     RepartitionSpec(st.key), st.line, st.col)
        else:
            reads = set().union(*(_cols(a.expr) for a in st.assigns))
            t = Task(i, Kind.UPDATE, st.table, frozenset(reads),
                     frozenset(a.column for a in st.assigns),
                     UpdateSpec(tuple((a.column, a.expr) for a in st.assigns)), st.line, st.col)
        tasks.append(t)
    return tasks
