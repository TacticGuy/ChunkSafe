"""Compile-time introspection of the target SQLite database (read-only)."""
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class TableInfo:
    name: str
    create_sql: str
    columns: List[str]
    ipk: Optional[str]                      # INTEGER PRIMARY KEY column (rowid alias), if any
    unique_cols: Set[str]                   # columns in a PK / UNIQUE constraint or unique index
    aux_sql: List[str] = field(default_factory=list)   # indexes + triggers to recreate after swap
    key_range: Optional[Tuple[int, int]] = None        # (min rowid, max rowid); None if empty


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def introspect(path: str, tables) -> Dict[str, TableInfo]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = {}
        for t in sorted(set(tables)):
            sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0]
            info = list(con.execute(f"PRAGMA table_info({_q(t)})"))
            cols = [r[1] for r in info]
            pks = [r for r in info if r[5] > 0]
            ipk = pks[0][1] if len(pks) == 1 and pks[0][2].upper() == "INTEGER" else None
            unique = {r[1] for r in pks}
            for idx in con.execute(f"PRAGMA index_list({_q(t)})").fetchall():
                if idx[2]:  # unique
                    unique |= {r[2] for r in con.execute(f"PRAGMA index_info({_q(idx[1])})")}
            aux = [r[0] for r in con.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') "
                "AND sql IS NOT NULL ORDER BY type DESC, name", (t,))]
            lo, hi = con.execute(f"SELECT MIN(rowid), MAX(rowid) FROM {_q(t)}").fetchone()
            out[t] = TableInfo(t, sql, cols, ipk, unique, aux, None if lo is None else (lo, hi))
        return out
    finally:
        con.close()
