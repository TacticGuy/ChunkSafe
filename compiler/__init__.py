"""ChunkSafe compiler package."""
from typing import List

from . import parser, semantic
from .ir import Task, lower
from .passes import safety


def compile_frontend(source: str, catalog: "semantic.Catalog") -> List[Task]:
    """Phase 1 pipeline. Returns verified, retry-safe IR tasks (Handoff 1 -> 2)."""
    program = semantic.check(parser.parse(source), catalog)
    return safety.check(lower(program))


def compile_to_sql(source: str, db_path: str, drop_prob: float, **sizing_params) -> str:
    """Full pipeline: source -> chunked SQLite SQL. Raises CompileError before touching the DB."""
    import hashlib

    from . import codegen, dbinfo
    from .passes import sizing

    tasks = compile_frontend(source, semantic.Catalog.from_sqlite(db_path))
    infos = dbinfo.introspect(db_path, {t.table for t in tasks})
    sizing.size(tasks, {t: i.key_range for t, i in infos.items()}, drop_prob, **sizing_params)
    job = hashlib.sha1((source + repr(sorted(sizing_params.items())) + str(drop_prob)).encode()).hexdigest()[:8]
    return codegen.generate(tasks, infos, job)
