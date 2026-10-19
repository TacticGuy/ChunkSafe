"""ChunkSafe evaluation.

    python evaluate.py              # full run, results in out/eval/
    python evaluate.py --quick      # fewer trials, for a fast check

Four parts:
  1. Safety pass as a classifier: confusion matrix, precision, recall, F1.
     Ground truth comes from an oracle: run the program twice with plain SQL
     and check whether the second run changes anything.
  2. Crash correctness: kill the real runner at every block boundary and at
     random points, then compare with a one-shot reorganization.
  3. Chunk sizing: Monte Carlo simulation of Poisson connectivity drops.
     Compares Young's chunk size with fixed chunk sizes (the U-curve), and
     checks what happens when the predicted drop probability is wrong.
  4. Cost: runtime overhead vs. one-shot SQL, peak disk use, compile time.
"""
import argparse
import csv
import json
import math
import random
import shutil
import sqlite3
import statistics
import sys
import time
import warnings
from pathlib import Path

from compiler import compile_frontend, compile_to_sql, parser
from compiler import ast_nodes as A
from compiler.codegen import q, render_expr
from compiler.errors import UnsafeRetryError
from compiler.passes import sizing
from compiler.runner import Interrupted, parse_blocks, run
from compiler.semantic import Catalog

ROOT = Path(__file__).resolve().parent


def banner(title):
    print("\n" + "=" * 70 + "\n" + title + "\n" + "=" * 70)


# ---------------------------------------------------------------------------
# Part 1: safety pass as a classifier
# ---------------------------------------------------------------------------
# Positive class = UNSAFE (not idempotent). Labels come from the oracle, not by hand.
SAFETY_CASES = [
    # plain single statements
    "UPDATE stats SET visits = 0;",
    "UPDATE stats SET views = visits * 2;",
    "UPDATE stats SET views = visits + 1;",
    "UPDATE stats SET views = 5;",
    "UPDATE stats SET visits = views - 3;",
    "UPDATE stats SET views = (visits + 3) * 2;",
    "UPDATE stats SET visits = 'n/a';",
    "UPDATE stats SET views = visits / 2;",
    "UPDATE stats SET visits = 0, views = 0;",
    "UPDATE stats SET views = id * 3;",
    "UPDATE stats SET visits = id - id;",
    "UPDATE stats SET views = -visits;",
    "UPDATE stats SET visits = visits + 1;",
    "UPDATE stats SET visits = visits * 2;",
    "UPDATE stats SET visits = visits - 1;",
    "UPDATE stats SET visits = -visits;",
    "UPDATE stats SET visits = visits * visits;",
    "UPDATE stats SET visits = views, views = visits;",
    "UPDATE stats SET visits = visits + views;",
    "UPDATE stats SET views = views * 2 + visits;",
    "UPDATE stats SET visits = (visits + 1) * 2;",
    "UPDATE stats SET visits = visits / 2;",
    "UPDATE stats SET views = visits, visits = visits + 1;",
    "UPDATE stats SET views = views + 1, visits = 7;",
    # idempotent in fact, but they read the column they write
    "UPDATE stats SET visits = visits * 1;",
    "UPDATE stats SET visits = visits + 0;",
    "UPDATE stats SET visits = visits - visits + 5;",
    "UPDATE stats SET visits = visits / 1;",
    "UPDATE stats SET visits = visits * 0;",
    "UPDATE stats SET views = views - views;",
    "UPDATE stats SET visits = visits;",
    "UPDATE stats SET views = views * 0 + visits;",
    # REINDEX / REPARTITION never change data
    "REINDEX stats ON (visits);",
    "REPARTITION stats BY views;",
    # multi-statement programs: whole-script rerun matters (design Rule 3)
    "UPDATE stats SET visits = 0; UPDATE stats SET views = visits * 2;",
    "UPDATE stats SET views = 1; UPDATE stats SET visits = views;",
    "UPDATE stats SET views = visits * 2; UPDATE stats SET visits = 0;",
    "UPDATE stats SET views = visits + 1; UPDATE stats SET visits = views;",
    "REINDEX stats ON (visits); UPDATE stats SET visits = 0;",
    "UPDATE stats SET views = visits; UPDATE stats SET visits = id;",
]


def _stats_db(path):
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE stats(id INTEGER PRIMARY KEY, visits INTEGER, views INTEGER);")
    rnd = random.Random(7)
    vals = [0, 1, -1, 2, 3, 10, 99, -50, 1000] + [rnd.randint(-500, 500) for _ in range(40)]
    con.executemany("INSERT INTO stats VALUES(?,?,?)",
                    [(i + 1, v, rnd.randint(-100, 100)) for i, v in enumerate(vals)])
    con.commit()
    con.close()


def _plain_sql(program):
    """Translate a ChunkSafe program to ordinary one-shot SQL (the oracle's version)."""
    out = []
    for st in program.statements:
        if isinstance(st, A.Update):
            sets = ", ".join(f"{q(a.column)} = {render_expr(a.expr)}" for a in st.assigns)
            out.append(f"UPDATE {q(st.table)} SET {sets};")
        elif isinstance(st, A.Reindex):
            out.append(f"CREATE INDEX IF NOT EXISTS {q('o_' + '_'.join(st.columns))} "
                       f"ON {q(st.table)}({', '.join(map(q, st.columns))});")
        else:
            out.append(f"CREATE INDEX IF NOT EXISTS {q('o_p_' + st.key)} ON {q(st.table)}({q(st.key)});")
    return "\n".join(out)


def oracle_unsafe(src, template):
    """True if running the program a second time changes the data: f(f(x)) != f(x)."""
    db = template.parent / "oracle.sqlite"
    shutil.copy(template, db)
    con = sqlite3.connect(db)
    sql = _plain_sql(parser.parse(src))
    con.executescript(sql)
    once = con.execute("SELECT * FROM stats ORDER BY id").fetchall()
    con.executescript(sql)
    twice = con.execute("SELECT * FROM stats ORDER BY id").fetchall()
    con.close()
    return once != twice


def compiler_verdict(src, catalog):
    """'reject' (UnsafeRetryError), 'warn' (accepted with Rule 3 warning) or 'accept'."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        try:
            compile_frontend(src, catalog)
        except UnsafeRetryError:
            return "reject"
    return "warn" if w else "accept"


def scores(pairs):
    tp = sum(1 for y, p in pairs if y and p)
    fp = sum(1 for y, p in pairs if not y and p)
    fn = sum(1 for y, p in pairs if y and not p)
    tn = sum(1 for y, p in pairs if not y and not p)
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    acc = (tp + tn) / len(pairs)
    fpr = fp / (fp + tn) if fp + tn else 0.0
    return dict(tp=tp, fp=fp, fn=fn, tn=tn, precision=prec, recall=rec, f1=f1,
                accuracy=acc, fpr=fpr)


def eval_safety(work, cases=SAFETY_CASES):
    banner("1. Safety pass as a classifier (positive = UNSAFE)")
    template = work / "stats_template.sqlite"
    if template.exists():
        template.unlink()
    _stats_db(template)
    catalog = Catalog.from_sqlite(str(template))
    rows = []
    for src in cases:
        rows.append(dict(program=src, truth="unsafe" if oracle_unsafe(src, template) else "safe",
                         verdict=compiler_verdict(src, catalog),
                         statements=src.count(";")))
    single = [r for r in rows if r["statements"] == 1]
    multi = [r for r in rows if r["statements"] > 1]

    def pairs(rs, count_warn):
        flagged = ("reject", "warn") if count_warn else ("reject",)
        return [(r["truth"] == "unsafe", r["verdict"] in flagged) for r in rs]

    res = {
        "single_statement": scores(pairs(single, False)),
        "all_errors_only": scores(pairs(rows, False)),
        "all_errors_or_warnings": scores(pairs(rows, True)),
    }
    print(f"{len(rows)} programs ({len(single)} single-statement, {len(multi)} multi-statement)\n")
    print(f"{'set':<34}{'TP':>4}{'FP':>4}{'FN':>4}{'TN':>4}{'prec':>7}{'recall':>8}{'F1':>7}{'FPR':>7}")
    names = {"single_statement": "single statements (errors)",
             "all_errors_only": "all programs (errors only)",
             "all_errors_or_warnings": "all programs (errors + warnings)"}
    for k, s in res.items():
        print(f"{names[k]:<34}{s['tp']:>4}{s['fp']:>4}{s['fn']:>4}{s['tn']:>4}"
              f"{s['precision']:>7.2f}{s['recall']:>8.2f}{s['f1']:>7.2f}{s['fpr']:>7.2f}")
    print("\nmistakes:")
    for r in rows:
        y = r["truth"] == "unsafe"
        if y != (r["verdict"] == "reject"):
            kind = "FN" if y else "FP"
            print(f"  {kind}  truth={r['truth']:<6} verdict={r['verdict']:<6} {r['program']}")
    res["cases"] = rows
    return res


# ---------------------------------------------------------------------------
# Part 2: crash correctness on the real runner
# ---------------------------------------------------------------------------
JOB = """REINDEX orders ON (region, ts);
REPARTITION trips BY zone;
UPDATE stats SET views = visits * 2;
UPDATE orders SET total = id * 2;
"""
REFERENCE = """CREATE INDEX cs_idx_orders_region_ts ON orders(region, ts);
CREATE INDEX cs_part_trips_zone ON trips(zone);
UPDATE stats SET views = visits * 2;
UPDATE orders SET total = id * 2;
"""


def build_db(path, n_orders):
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE orders(id INTEGER PRIMARY KEY, region TEXT, ts INTEGER, total INTEGER);
        CREATE INDEX idx_orders_region ON orders(region);
        CREATE TABLE trips(zone TEXT, dist REAL);
        CREATE TABLE stats(id INTEGER PRIMARY KEY, visits INTEGER, views INTEGER);
    """)
    con.executemany("INSERT INTO orders VALUES(?,?,?,0)",
                    [(i, f"r{i % 7}", i * 10) for i in range(1, n_orders + 1)])
    con.executemany("INSERT INTO trips VALUES(?,?)", [(f"z{i % 5}", i / 3) for i in range(n_orders // 10)])
    con.executemany("INSERT INTO stats VALUES(?,?,0)", [(i, i * 3) for i in range(1, n_orders // 20 + 1)])
    con.commit()
    con.close()


def snapshot(path):
    con = sqlite3.connect(path)
    try:
        data = {t: con.execute(f"SELECT rowid, * FROM {t} ORDER BY rowid").fetchall()
                for t in ("orders", "trips", "stats")}
        idx = sorted(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"))
        left = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '\\_cs\\_shadow%' ESCAPE '\\'")]
        ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return data, idx, left, ok
    finally:
        con.close()


def eval_correctness(work, random_trials):
    banner("2. Crash correctness (real runner, real SQLite)")
    base = work / "crash_base.sqlite"
    build_db(base, 4000)
    sql = compile_to_sql(JOB, str(base), 0.9, throughput=200)
    total = len(parse_blocks(sql)[1])
    ref = work / "crash_ref.sqlite"
    shutil.copy(base, ref)
    con = sqlite3.connect(ref)
    con.executescript(REFERENCE)
    con.close()
    want = snapshot(ref)

    def trial(schedule):
        db = work / "crash_trial.sqlite"
        shutil.copy(base, db)
        kills = 0
        for n in schedule:
            try:
                run(str(db), sql, max_blocks=n)
                break
            except Interrupted:
                kills += 1
        else:
            run(str(db), sql)
        got = snapshot(db)
        return dict(match=got[0] == want[0] and got[1] == want[1], leftover=bool(got[2]),
                    integrity=got[3], kills=kills)

    every = [trial([n]) for n in range(total)]
    rnd = random.Random(1)
    rand = [trial([rnd.randint(0, 6) for _ in range(400)]) for _ in range(random_trials)]
    res = {}
    for name, rs in (("every_boundary", every), ("random_schedules", rand)):
        res[name] = dict(trials=len(rs),
                         match_rate=sum(r["match"] for r in rs) / len(rs),
                         leftover_rate=sum(r["leftover"] for r in rs) / len(rs),
                         integrity_rate=sum(r["integrity"] for r in rs) / len(rs),
                         total_kills=sum(r["kills"] for r in rs))
    res["blocks_in_job"] = total
    print(f"job: 4 tasks, {total} blocks\n")
    print(f"{'experiment':<22}{'trials':>7}{'kills':>8}{'match':>8}{'leftover':>10}{'integrity':>11}")
    for k in ("every_boundary", "random_schedules"):
        r = res[k]
        print(f"{k:<22}{r['trials']:>7}{r['total_kills']:>8}{r['match_rate']:>8.0%}"
              f"{r['leftover_rate']:>10.0%}{r['integrity_rate']:>11.0%}")
    print(f"\nkill-point coverage: {total}/{total} block boundaries = 100%")
    return res


# ---------------------------------------------------------------------------
# Part 3: chunk sizing under simulated drops
# ---------------------------------------------------------------------------
def simulate(total_rows, chunk_rows, r, c, d, rng, restart=0.0):
    """One job. Drops are Poisson with mean gap d seconds. A drop loses the chunk in progress.
    Returns (completion time, wasted seconds, drops, chunks committed)."""
    t = wasted = 0.0
    done = drops = commits = 0
    next_drop = rng.expovariate(1.0 / d)
    while done < total_rows:
        n = min(chunk_rows, total_rows - done)
        dur = n / r + c
        if t + dur > next_drop:
            wasted += next_drop - t
            t = next_drop + restart
            drops += 1
            next_drop = t + rng.expovariate(1.0 / d)
        else:
            t += dur
            done += n
            commits += 1
    return t, wasted, drops, commits


def eval_sizing(work, trials, total_rows=200_000, r=5000.0, c=0.02, window=60.0):
    banner("3. Chunk sizing: Young's formula vs. fixed chunk sizes (Monte Carlo)")
    print(f"job = {total_rows:,} rows, r = {r:.0f} rows/s ({total_rows / r:.0f} s of pure work), "
          f"C = {c} s, {trials} trials per point\n")
    grid = sorted({int(round(10 ** (2 + i * 0.125))) for i in range(30) if 10 ** (2 + i * 0.125) <= total_rows}
                  | {total_rows})
    curves, summary = [], []
    for p in (0.1, 0.5, 0.9):
        d = sizing.expected_drop_time(p, window)
        young_rows, t_opt = sizing.chunk_rows_for(p, checkpoint_cost=c, throughput=r, window=window,
                                                  max_rows=total_rows)

        def measure(rows):
            rng = random.Random(hash((p, rows)) & 0xFFFF)
            sims = [simulate(total_rows, rows, r, c, d, rng) for _ in range(trials)]
            ts = [s[0] for s in sims]
            return dict(p=p, chunk_rows=rows, mean_time=statistics.mean(ts),
                        p95_time=sorted(ts)[int(0.95 * len(ts)) - 1],
                        wasted=statistics.mean(s[1] for s in sims),
                        drops=statistics.mean(s[2] for s in sims),
                        overhead=statistics.mean(s[3] * c / s[0] for s in sims))

        pts = [measure(rows) for rows in grid]
        y = measure(young_rows)
        curves += pts
        best = min(pts, key=lambda x: x["mean_time"])
        one = next(x for x in pts if x["chunk_rows"] == total_rows)
        tiny = pts[0]
        summary.append(dict(p=p, D=d, young_rows=young_rows, young_time=y["mean_time"],
                            young_p95=y["p95_time"], young_wasted=y["wasted"], young_overhead=y["overhead"],
                            best_rows=best["chunk_rows"], best_time=best["mean_time"],
                            gap_to_best=y["mean_time"] / best["mean_time"] - 1,
                            no_chunking_time=one["mean_time"], tiny_rows=tiny["chunk_rows"],
                            tiny_time=tiny["mean_time"]))
    print(f"{'p':>4}{'D (s)':>8}{'Young rows':>12}{'Young time':>12}{'best grid':>11}{'best time':>11}"
          f"{'gap':>7}{'no chunks':>11}{'100 rows':>10}")
    for s in summary:
        print(f"{s['p']:>4}{s['D']:>8.1f}{s['young_rows']:>12,}{s['young_time']:>11.1f}s{s['best_rows']:>11,}"
              f"{s['best_time']:>10.1f}s{s['gap_to_best']:>7.1%}{s['no_chunking_time']:>10.0f}s{s['tiny_time']:>9.1f}s")
    print("\n'gap' = how much slower Young's size is than the best size found by brute force.")

    # sensitivity: compile for p_pred, reality is p_true
    ps = (0.1, 0.3, 0.5, 0.7, 0.9)
    sens = []
    for p_true in ps:
        d = sizing.expected_drop_time(p_true, window)
        times = {}
        for p_pred in ps:
            rows, _ = sizing.chunk_rows_for(p_pred, checkpoint_cost=c, throughput=r, window=window,
                                            max_rows=total_rows)
            rng = random.Random(int(p_true * 100) * 1000 + int(p_pred * 100))
            times[p_pred] = statistics.mean(simulate(total_rows, rows, r, c, d, rng)[0] for _ in range(trials))
        for p_pred in ps:
            sens.append(dict(p_true=p_true, p_pred=p_pred, slowdown=times[p_pred] / times[p_true] - 1))
    print("\nSensitivity: slowdown when the predicted p is wrong (rows = predicted, columns = true)")
    print("pred\\true" + "".join(f"{p:>8}" for p in ps))
    for p_pred in ps:
        print(f"{p_pred:>9}" + "".join(
            f"{next(x['slowdown'] for x in sens if x['p_true'] == pt and x['p_pred'] == p_pred):>8.1%}" for pt in ps))
    return dict(summary=summary, curves=curves, sensitivity=sens)


# ---------------------------------------------------------------------------
# Part 4: real cost on this machine
# ---------------------------------------------------------------------------
def eval_cost(work, n_orders):
    banner("4. Cost on this machine: ChunkSafe vs. one-shot SQL")
    res = []
    jobs = [("REINDEX orders ON (region, ts);", "CREATE INDEX x ON orders(region, ts);"),
            ("REPARTITION orders BY region;", "CREATE INDEX x ON orders(region);"),
            ("UPDATE orders SET total = id * 2;", "UPDATE orders SET total = id * 2;")]
    base = work / "cost_base.sqlite"
    build_db(base, n_orders)
    base_size = base.stat().st_size
    for src, plain in jobs:
        a = work / "cost_plain.sqlite"
        shutil.copy(base, a)
        con = sqlite3.connect(a)
        t0 = time.perf_counter()
        con.executescript(plain)
        con.commit()
        plain_t = time.perf_counter() - t0
        con.close()

        b = work / "cost_cs.sqlite"
        shutil.copy(base, b)
        t0 = time.perf_counter()
        sql = compile_to_sql(src, str(b), 0.5)
        compile_t = time.perf_counter() - t0
        peak = [b.stat().st_size]
        t0 = time.perf_counter()
        run(str(b), sql, after_block=lambda _n: peak.append(b.stat().st_size))
        cs_t = time.perf_counter() - t0
        chunks = sql.count("-- @block type=chunk")
        res.append(dict(job=src, rows=n_orders, chunks=chunks, plain_s=plain_t, chunksafe_s=cs_t,
                        overhead_x=cs_t / plain_t, compile_ms=compile_t * 1000,
                        base_mb=base_size / 1e6, peak_mb=max(peak) / 1e6,
                        measured_rows_per_s=n_orders / cs_t, sql_kb=len(sql) / 1000))
    print(f"table: orders, {n_orders:,} rows, DB file {base_size / 1e6:.1f} MB, compiled with p = 0.5\n")
    print(f"{'job':<36}{'chunks':>7}{'plain':>8}{'ChunkSafe':>11}{'overhead':>10}{'compile':>9}{'peak disk':>11}")
    for x in res:
        print(f"{x['job']:<36}{x['chunks']:>7}{x['plain_s']:>7.2f}s{x['chunksafe_s']:>10.2f}s"
              f"{x['overhead_x']:>9.1f}x{x['compile_ms']:>7.0f}ms{x['peak_mb'] / x['base_mb']:>10.2f}x")
    print(f"\nmeasured throughput: {min(x['measured_rows_per_s'] for x in res):,.0f} to "
          f"{max(x['measured_rows_per_s'] for x in res):,.0f} rows/s (sizing assumes 5,000 by default)")
    return res


# ---------------------------------------------------------------------------
def write_outputs(out, results):
    (out / "results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    with open(out / "sizing_curve.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(results["sizing"]["curves"][0]))
        w.writeheader()
        w.writerows(results["sizing"]["curves"])
    with open(out / "safety_cases.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["program", "truth", "verdict", "statements"])
        w.writeheader()
        w.writerows(results["safety"]["cases"])
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("\n(matplotlib not installed: skipped PNG chart; CSVs are written)")
        return
    fig, ax = plt.subplots(figsize=(8, 5))
    for s in results["sizing"]["summary"]:
        pts = [x for x in results["sizing"]["curves"] if x["p"] == s["p"]]
        line, = ax.plot([x["chunk_rows"] for x in pts], [x["mean_time"] for x in pts], label=f"p = {s['p']}")
        ax.scatter([s["young_rows"]], [s["young_time"]], color=line.get_color(), zorder=5, s=50, marker="D")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("rows per chunk")
    ax.set_ylabel("mean completion time (s)")
    ax.set_title("Completion time vs chunk size (diamond = Young's choice)")
    ax.legend()
    ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out / "sizing_curve.png", dpi=150)
    print(f"\nchart: {out / 'sizing_curve.png'}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="ChunkSafe evaluation")
    ap.add_argument("--out", default=str(ROOT / "out" / "eval"))
    ap.add_argument("--quick", action="store_true", help="fewer trials")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    results = {
        "safety": eval_safety(out),
        "correctness": eval_correctness(out, 10 if args.quick else 30),
        "sizing": eval_sizing(out, 40 if args.quick else 150),
        "cost": eval_cost(out, 20_000 if args.quick else 200_000),
    }
    write_outputs(out, results)
    banner(f"done in {time.time() - t0:.0f} s, results in {out}")
    ok = (results["safety"]["single_statement"]["recall"] == 1.0
          and all(results["correctness"][k]["match_rate"] == 1.0 for k in ("every_boundary", "random_schedules")))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
