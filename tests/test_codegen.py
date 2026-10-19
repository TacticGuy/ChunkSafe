import sqlite3

import pytest
from compiler import compile_to_sql
from compiler.errors import CodegenError, SemanticError, UnsafeRetryError
from compiler.runner import parse_blocks, run

from .helpers import make_db


def test_generated_sql_structure(tmp_path):
    db = make_db(tmp_path / "a.sqlite")
    sql = compile_to_sql("REINDEX orders ON (region, ts);", db, 0.9, throughput=100)
    job, blocks = parse_blocks(sql)
    kinds = [b.kind for b in blocks]
    assert kinds[:2] == ["init", "prepare"] and kinds[-1] == "swap"
    assert kinds.count("chunk") > 1
    assert "INSERT OR REPLACE" in sql and "DROP TABLE" in sql and "RENAME TO" in sql


def test_runs_top_to_bottom_with_plain_sqlite(tmp_path):
    db = make_db(tmp_path / "a.sqlite")
    sql = compile_to_sql("REPARTITION trips BY zone;", db, 0.5)
    con = sqlite3.connect(db, isolation_level=None)
    con.executescript(sql)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert "cs_part_trips_zone" in names and not any(n.startswith("_cs_shadow") for n in names)
    assert con.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 500


def test_replaying_finished_script_is_refused_not_destructive(tmp_path):
    db = make_db(tmp_path / "a.sqlite")
    sql = compile_to_sql("UPDATE stats SET views = visits * 2;", db, 0.5)
    run(db, sql)
    before = sqlite3.connect(db).execute("SELECT SUM(views) FROM stats").fetchone()
    assert run(db, sql) == 1                       # runner: only the idempotent init block re-runs
    con = sqlite3.connect(db, isolation_level=None)
    with pytest.raises(sqlite3.IntegrityError):    # raw replay hits the swap guard
        con.executescript(sql)
    if con.in_transaction:
        con.execute("ROLLBACK")
    assert con.execute("SELECT SUM(views) FROM stats").fetchone() == before


def test_unsafe_program_fails_without_touching_db(tmp_path):
    db = make_db(tmp_path / "a.sqlite")
    snapshot = open(db, "rb").read()
    with pytest.raises(UnsafeRetryError):
        compile_to_sql("UPDATE stats SET visits = visits + 1;", db, 0.5)
    with pytest.raises(SemanticError):
        compile_to_sql("REINDEX ghost ON (a);", db, 0.5)
    assert open(db, "rb").read() == snapshot


def test_updating_unique_column_rejected(tmp_path):
    db = make_db(tmp_path / "a.sqlite")
    with pytest.raises(CodegenError):
        compile_to_sql("UPDATE orders SET id = 1;", db, 0.5)
