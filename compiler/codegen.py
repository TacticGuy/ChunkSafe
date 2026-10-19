"""SQLite code generation: shadow table + chunked INSERT OR REPLACE + atomic swap.

Output is plain SQL split into blocks by marker comments (`-- @block type=... task=... idx=...`)
so runner.py can skip finished work. Each block is its own transaction.
"""
import re
from typing import Dict, List

from . import ast_nodes as A
from .dbinfo import TableInfo
from .errors import CodegenError
from .ir import Kind, Task


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def render_expr(e) -> str:
    if isinstance(e, A.Num):
        return str(int(e.value)) if float(e.value).is_integer() else repr(e.value)
    if isinstance(e, A.Str):
        return lit(e.value)
    if isinstance(e, A.ColRef):
        return q(e.name)
    return f"({render_expr(e.left)} {e.op} {render_expr(e.right)})"


_CREATE_TABLE = re.compile(r'^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?("[^"]+"|`[^`]+`|\[[^\]]+\]|\w+)', re.I)


def _shadow_ddl(info: TableInfo, shadow: str) -> str:
    if re.search(r"WITHOUT\s+ROWID", info.create_sql, re.I):
        raise CodegenError(f"table '{info.name}' is WITHOUT ROWID; not supported")
    m = _CREATE_TABLE.match(info.create_sql)
    if not m:
        raise CodegenError(f"cannot parse CREATE TABLE for '{info.name}'")
    return f"CREATE TABLE IF NOT EXISTS {q(shadow)}" + info.create_sql[m.end():] + ";"


def _idempotent(sql: str) -> str:
    sql = re.sub(r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\s+(?!IF\s+NOT)", lambda m: f"CREATE {m.group(1) or ''}INDEX IF NOT EXISTS ", sql, flags=re.I)
    return re.sub(r"^\s*CREATE\s+TRIGGER\s+(?!IF\s+NOT)", "CREATE TRIGGER IF NOT EXISTS ", sql, flags=re.I)


def _new_index(task: Task) -> str:
    t = task.table
    if task.kind is Kind.REINDEX:
        cols = task.spec.index_cols
        return f"CREATE INDEX IF NOT EXISTS {q('cs_idx_' + t + '_' + '_'.join(cols))} ON {q(t)}({', '.join(map(q, cols))})"
    if task.kind is Kind.REPARTITION:
        k = task.spec.key
        return f"CREATE INDEX IF NOT EXISTS {q('cs_part_' + t + '_' + k)} ON {q(t)}({q(k)})"
    return ""


def _copy_select(task: Task, info: TableInfo):
    """Return (insert column list, select expression list) for one chunk copy."""
    assigned = {}
    if task.kind is Kind.UPDATE:
        assigned = dict(task.spec.assignments)
        bad = sorted(set(assigned) & info.unique_cols)
        if bad:
            raise CodegenError(
                f"task {task.id} updates key/unique column(s) {bad} of '{task.table}'; "
                "INSERT OR REPLACE could collapse rows", task.line, task.col)
    cols = list(info.columns)
    sel = [f"({render_expr(assigned[c])}) AS {q(c)}" if c in assigned else q(c) for c in cols]
    if info.ipk is None:  # carry the implicit rowid so replays hit the same row
        cols, sel = ["rowid"] + [q(c) for c in cols], ["rowid"] + sel
    else:
        cols = [q(c) for c in cols]
    return cols, sel


def generate(tasks: List[Task], infos: Dict[str, TableInfo], job: str) -> str:
    out = [f"-- ChunkSafe generated SQL", f"-- @job {job}",
           "-- @block type=init",
           "CREATE TABLE IF NOT EXISTS _cs_progress(job TEXT NOT NULL, task INTEGER NOT NULL, "
           "chunk INTEGER NOT NULL, status TEXT NOT NULL, PRIMARY KEY(job, task, chunk));",
           "CREATE TABLE IF NOT EXISTS _cs_guard(ok INTEGER CHECK(ok = 1));", ""]
    # indexes/triggers to recreate per table; grows as earlier tasks add indexes
    aux = {t: [_idempotent(s) for s in i.aux_sql] for t, i in infos.items()}
    J = lit(job)

    for task in tasks:
        info, k = infos[task.table], task.id
        shadow = f"_cs_shadow_{job}_{k}"
        cols, sel = _copy_select(task, info)
        out += [f"-- @block type=prepare task={k}", _shadow_ddl(info, shadow), ""]

        for c in task.chunks:
            done = f"SELECT 1 FROM _cs_progress WHERE job = {J} AND task = {k} AND chunk = {c.idx}"
            out += [f"-- @block type=chunk task={k} idx={c.idx}", "BEGIN IMMEDIATE;",
                    f"INSERT OR REPLACE INTO {q(shadow)}({', '.join(cols)})",
                    f"  SELECT {', '.join(sel)} FROM {q(task.table)}",
                    f"  WHERE rowid >= {c.lo} AND rowid < {c.hi} AND NOT EXISTS ({done});",
                    f"INSERT OR REPLACE INTO _cs_progress VALUES ({J}, {k}, {c.idx}, 'done');",
                    "COMMIT;", ""]

        new_idx = _new_index(task)
        recreate = aux[task.table] + ([new_idx] if new_idx else [])
        if new_idx:
            aux[task.table].append(new_idx)
        out += [f"-- @block type=swap task={k}", "PRAGMA legacy_alter_table = ON;", "BEGIN IMMEDIATE;",
                # abort (CHECK failure) unless every chunk is done and the swap has not happened yet
                f"INSERT INTO _cs_guard(ok) SELECT 0 WHERE (SELECT COUNT(*) FROM _cs_progress "
                f"WHERE job = {J} AND task = {k} AND chunk >= 0 AND status = 'done') <> {len(task.chunks)};",
                f"INSERT INTO _cs_guard(ok) SELECT 0 WHERE EXISTS (SELECT 1 FROM _cs_progress "
                f"WHERE job = {J} AND task = {k} AND chunk = -1);",
                f"DROP TABLE {q(task.table)};",
                f"ALTER TABLE {q(shadow)} RENAME TO {q(task.table)};"]
        out += [s.rstrip().rstrip(";") + ";" for s in recreate]
        out += [f"INSERT OR REPLACE INTO _cs_progress VALUES ({J}, {k}, -1, 'swapped');",
                "COMMIT;", "PRAGMA legacy_alter_table = OFF;", ""]
    return "\n".join(out)
