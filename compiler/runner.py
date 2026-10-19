"""Resume runner: executes generated SQL block by block, skipping finished work."""
import re
import sqlite3
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

_MARK = re.compile(r"^-- @block (.*)$")


class Interrupted(Exception):
    """Simulated connectivity drop between blocks."""


@dataclass
class Block:
    kind: str
    task: Optional[int]
    idx: Optional[int]
    text: str


def parse_blocks(sql: str) -> Tuple[str, List[Block]]:
    job = re.search(r"^-- @job (\S+)", sql, re.M).group(1)
    blocks, cur, attrs = [], [], None

    def flush():
        if attrs is not None:
            blocks.append(Block(attrs["type"], int(attrs["task"]) if "task" in attrs else None,
                                int(attrs["idx"]) if "idx" in attrs else None, "\n".join(cur)))

    for line in sql.splitlines():
        m = _MARK.match(line)
        if m:
            flush()
            attrs, cur = dict(p.split("=") for p in m.group(1).split()), []
        else:
            cur.append(line)
    flush()
    return job, blocks


def run(db_path: str, sql: str, max_blocks: Optional[int] = None,
        after_block: Optional[Callable[[int], None]] = None) -> int:
    """Run (or resume) the job. Returns number of blocks executed.

    max_blocks: raise Interrupted once this many blocks ran (simulated drop).
    after_block(n): called after each executed block (tests use it to kill the process).
    """
    job, blocks = parse_blocks(sql)
    con = sqlite3.connect(db_path, isolation_level=None)
    executed = 0
    try:
        for b in blocks:
            if b.kind != "init":
                if _exists(con, job, b.task, -1):
                    continue                                   # task already swapped
                if b.kind == "chunk" and _exists(con, job, b.task, b.idx):
                    continue                                   # chunk already committed
            if max_blocks is not None and executed >= max_blocks:
                raise Interrupted(f"dropped after {executed} blocks")
            try:
                con.executescript(b.text)
            except Exception:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            executed += 1
            if after_block:
                after_block(executed)
        return executed
    finally:
        con.close()


def _exists(con, job, task, chunk) -> bool:
    return con.execute("SELECT 1 FROM _cs_progress WHERE job=? AND task=? AND chunk=?",
                       (job, task, chunk)).fetchone() is not None
