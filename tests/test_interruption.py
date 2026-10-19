"""Interruption simulation: kill at every block boundary, resume, compare to a one-shot reference."""
import os
import shutil
import subprocess
import sys
import sqlite3
from pathlib import Path

import pytest
from compiler import compile_to_sql
from compiler.runner import Interrupted, parse_blocks, run

from .helpers import SOURCE, make_db, reference, snapshot

ROOT = Path(__file__).resolve().parent.parent
PARAMS = dict(throughput=100)  # tiny chunks => many blocks


@pytest.fixture
def setup(tmp_path):
    db = make_db(tmp_path / "app.sqlite")
    sql = compile_to_sql(SOURCE, db, 0.9, **PARAMS)
    ref = tmp_path / "ref.sqlite"
    shutil.copy(db, ref)
    reference(ref)
    total = len(parse_blocks(sql)[1])
    assert total > 20
    return db, sql, snapshot(ref), tmp_path, total


def check(db, expected):
    data, idx, leftovers = snapshot(db)
    assert data == expected[0]
    assert idx == expected[1]
    assert leftovers == []


def test_uninterrupted_matches_reference(setup):
    db, sql, expected, *_ = setup
    run(db, sql)
    check(db, expected)


def test_drop_at_every_block_boundary_then_resume(setup, tmp_path):
    db, sql, expected, tmp, total = setup
    for n in range(total):
        trial = tmp / f"trial{n}.sqlite"
        shutil.copy(db, trial)
        with pytest.raises(Interrupted):
            run(trial, sql, max_blocks=n)
        run(trial, sql)                                     # resume from last committed chunk
        check(trial, expected)


def test_repeated_drops_make_progress(setup):
    db, sql, expected, *_ = setup
    for _ in range(100):
        try:
            run(db, sql, max_blocks=3)
            break
        except Interrupted:
            continue
    else:
        pytest.fail("never finished")
    check(db, expected)


def test_crash_mid_transaction_rolls_back(setup):
    db, sql, expected, *_ = setup
    _, blocks = parse_blocks(sql)
    with pytest.raises(Interrupted):
        run(db, sql, max_blocks=2)                          # init + prepare
    chunk = next(b for b in blocks if b.kind == "chunk")
    con = sqlite3.connect(db, isolation_level=None)
    con.executescript(chunk.text.replace("COMMIT;", ""))    # power loss just before COMMIT
    con.close()                                             # uncommitted work is discarded
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM _cs_progress").fetchone()[0] == 0
    con.close()
    run(db, sql)
    check(db, expected)


def test_chunk_executed_twice_is_harmless(setup):
    db, sql, expected, *_ = setup
    _, blocks = parse_blocks(sql)
    with pytest.raises(Interrupted):
        run(db, sql, max_blocks=2)
    con = sqlite3.connect(db, isolation_level=None)
    for b in [x for x in blocks if x.kind == "chunk"][:3] * 2:
        con.executescript(b.text)
    con.close()
    run(db, sql)
    check(db, expected)


def test_real_process_kill_then_resume(setup):
    db, sql, expected, tmp, total = setup
    script = tmp / "job.sql"
    script.write_text(sql, encoding="utf-8")
    cmd = [sys.executable, "-m", "compiler.cli", "run", str(script), "--db", str(db)]
    for kill_after in (1, 5, 12, 20):
        r = subprocess.run(cmd, cwd=ROOT, env={**os.environ, "CS_KILL_AFTER": str(kill_after)})
        assert r.returncode == 137                          # hard os._exit, no cleanup
    assert subprocess.run(cmd, cwd=ROOT).returncode == 0
    check(db, expected)
