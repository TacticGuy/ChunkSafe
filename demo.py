"""ChunkSafe end-to-end demo (Phase 3.3).

    python demo.py            # writes everything under out/demo/

Steps:
  1. Build a sample SQLite DB (orders, trips, stats).
  2. Show how the drop probability p changes chunk size (Young's formula).
  3. Show that an unsafe UPDATE is rejected and the DB is not touched.
  4. Run a real job, kill the process several times, resume, and check the
     result equals a one-shot reorganization.
"""
import argparse
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from compiler import compile_to_sql
from compiler.passes import sizing

ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"

JOB = """-- demo job: two index jobs and one safe update
REINDEX orders ON (region, ts);
REPARTITION trips BY zone;
UPDATE stats SET views = visits * 2;
"""


def banner(title):
    print()
    print("=" * 64)
    print(title)
    print("=" * 64)


def build_db(path, n_orders=50_000):
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE orders(id INTEGER PRIMARY KEY, region TEXT, ts INTEGER, total INTEGER);
        CREATE INDEX idx_orders_region ON orders(region);
        CREATE TABLE trips(zone TEXT, dist REAL);
        CREATE TABLE stats(id INTEGER PRIMARY KEY, visits INTEGER, views INTEGER);
    """)
    con.executemany("INSERT INTO orders VALUES(?,?,?,?)",
                    [(i, f"r{i % 7}", i * 10, i % 100) for i in range(1, n_orders + 1)])
    con.executemany("INSERT INTO trips VALUES(?,?)", [(f"z{i % 5}", i / 3) for i in range(5_000)])
    con.executemany("INSERT INTO stats VALUES(?,?,0)", [(i, i * 3) for i in range(1, 2_001)])
    con.commit()
    con.close()


def one_shot_reference(path):
    """Same effect as JOB, done with plain SQL in one go (no chunking)."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE INDEX cs_idx_orders_region_ts ON orders(region, ts);
        CREATE INDEX cs_part_trips_zone ON trips(zone);
        UPDATE stats SET views = visits * 2;
    """)
    con.commit()
    con.close()


def snapshot(path):
    con = sqlite3.connect(path)
    try:
        data = {t: con.execute(f"SELECT rowid, * FROM {t} ORDER BY rowid").fetchall()
                for t in ("orders", "trips", "stats")}
        idx = sorted(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"))
        shadows = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '\\_cs\\_shadow%' ESCAPE '\\'")]
        return data, idx, shadows
    finally:
        con.close()


def progress(path):
    con = sqlite3.connect(path)
    try:
        done = con.execute("SELECT COUNT(*) FROM _cs_progress WHERE chunk >= 0").fetchone()[0]
        swapped = con.execute("SELECT COUNT(*) FROM _cs_progress WHERE chunk = -1").fetchone()[0]
        return done, swapped
    except sqlite3.OperationalError:
        return 0, 0
    finally:
        con.close()


def cli(*args, env=None):
    return subprocess.run([sys.executable, "-m", "compiler.cli", *map(str, args)], cwd=ROOT,
                          env={**os.environ, **(env or {})}, capture_output=True, text=True)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def step_sizing(db):
    banner("1. Drop probability -> chunk size (Young's formula T = sqrt(2*C*D))")
    print("C = 0.02 s per commit, window W = 60 s, throughput r = 5000 rows/s, orders = 50,000 rows\n")
    print(f"{'p':>5} {'D (s)':>9} {'T_opt (s)':>10} {'rows/chunk':>11} {'chunks':>7}")
    counts = {}
    for p in (0.1, 0.3, 0.5, 0.7, 0.9):
        rows, t = sizing.chunk_rows_for(p)
        d = sizing.expected_drop_time(p, 60.0)
        sql = compile_to_sql("REINDEX orders ON (region, ts);", str(db), p)
        n = sql.count("-- @block type=chunk")
        counts[p] = n
        print(f"{p:>5} {d:>9.1f} {t:>10.2f} {rows:>11,} {n:>7}")
    print("\nHigher p (worse network) -> smaller chunks -> less work lost per drop.")
    assert counts[0.9] > counts[0.1]
    return counts


def step_unsafe(db, out):
    banner("2. Unsafe program is rejected at compile time")
    src = EXAMPLES / "unsafe_update.reorg"
    print("source:", src.read_text(encoding="utf-8-sig").strip())
    before = sha(db)
    target = out / "unsafe.sql"
    r = cli("compile", src, "--db", db, "--drop-prob", 0.5, "-o", target)
    print("exit code:", r.returncode)
    print("message  :", r.stderr.strip())
    untouched = sha(db) == before and not target.exists()
    print("DB untouched and no SQL written:", untouched)
    assert r.returncode == 2 and untouched
    return r.returncode


def step_interrupt(db, out, kill_after=8):
    banner("3. Kill the job again and again, then resume")
    ref = out / "reference.sqlite"
    shutil.copy(db, ref)
    one_shot_reference(ref)

    job_src = out / "job.reorg"
    job_src.write_text(JOB, encoding="utf-8")
    job_sql = out / "job.sql"
    # small throughput -> many chunks, so the kills land in the middle of the work
    r = cli("compile", job_src, "--db", db, "--drop-prob", 0.9, "--throughput", 1500, "-o", job_sql)
    assert r.returncode == 0, r.stderr
    print(r.stdout.strip())

    kills = 0
    while True:
        r = cli("run", job_sql, "--db", db, env={"CS_KILL_AFTER": str(kill_after)})
        done, swapped = progress(db)
        if r.returncode == 0:
            print(f"run {kills + 1}: finished   chunks done={done}, tasks swapped={swapped}")
            break
        assert r.returncode == 137, r.stderr
        kills += 1
        print(f"run {kills}: KILLED after {kill_after} blocks   chunks done={done}, tasks swapped={swapped}")
        assert kills < 200, "job never finished"

    got, want = snapshot(db), snapshot(ref)
    same = got[0] == want[0] and got[1] == want[1] and got[2] == []
    print(f"\nprocess killed {kills} times")
    print("indexes now:", ", ".join(got[1]))
    print("result equals one-shot reference:", same)
    assert same
    r = cli("run", job_sql, "--db", db)
    print("re-running the finished job:", r.stdout.strip(), "(only the safe init block)")
    assert snapshot(db) == got
    return kills


def main(argv=None):
    ap = argparse.ArgumentParser(description="ChunkSafe demo")
    ap.add_argument("--out", default=str(ROOT / "out" / "demo"), help="output folder (default out/demo)")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    db = out / "app.sqlite"
    build_db(db)
    print(f"sample DB: {db}")
    step_sizing(db)
    step_unsafe(db, out)
    kills = step_interrupt(db, out)
    banner("All demo checks passed")
    print(f"files are in {out}")
    return 0 if kills > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
