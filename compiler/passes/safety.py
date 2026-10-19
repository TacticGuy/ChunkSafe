"""Retry-Safety Pass (design.md section 7): Read/Write-set analysis."""
import warnings
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
    # Rule 3 (cont.): a later task writing what an earlier task reads means a full
    # rerun of the script would feed new values back. Allowed, but warned.
    for i, ti in enumerate(tasks):
        for tj in tasks[i + 1:]:
            back = sorted(ti.reads & tj.writes) if ti.table == tj.table else []
            if back:
                warnings.warn(f"task {tj.id} writes {back} of '{tj.table}' that earlier task {ti.id} "
                              "reads; rerunning the whole script would see the new values")
    # Rule 4 (purity) holds structurally: the grammar has no function calls.
    return tasks
