"""Retry-Safety Pass (design.md section 7): Read/Write-set analysis."""
from typing import List

from ..errors import UnsafeRetryError
from ..ir import Task


def check(tasks: List[Task]) -> List[Task]:
    # Rules 1 & 2: a task whose reads intersect its writes is not idempotent.
    for t in tasks:
        bad = sorted(t.reads & t.writes)
        if bad:
            cols = ", ".join(f"'{t.table}.{c}'" for c in bad)
            raise UnsafeRetryError(
                f"task {t.id} ({t.kind.value}) reads and writes {cols}; "
                "retrying a chunk would apply it more than once", t.line, t.col)
    # Rule 3: record ordering dependencies (earlier writes feeding later reads).
    for j, tj in enumerate(tasks):
        tj.deps = [ti.id for ti in tasks[:j] if ti.table == tj.table and ti.writes & tj.reads]
    # Rule 4 (purity) holds structurally: the grammar has no function calls.
    return tasks
