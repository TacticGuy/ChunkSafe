"""Command line interface.

  python -m compiler.cli compile job.reorg --db app.sqlite --drop-prob 0.3 -o job.sql
  python -m compiler.cli run job.sql --db app.sqlite      (env CS_KILL_AFTER=n kills the process after n blocks)
"""
import argparse
import os
import sys

from . import compile_to_sql, runner
from .errors import CompileError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="chunksafe")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compile", help="compile a .reorg file to chunked SQLite SQL")
    c.add_argument("source")
    c.add_argument("--db", required=True, help="SQLite database whose schema the program targets")
    c.add_argument("--drop-prob", type=float, required=True, help="P(connectivity drop) within --window, in (0,1)")
    c.add_argument("-o", "--output", required=True)
    c.add_argument("--window", type=float, default=60.0, help="reference window in seconds (default 60)")
    c.add_argument("--checkpoint-cost", type=float, default=0.02, help="seconds per chunk commit (default 0.02)")
    c.add_argument("--throughput", type=float, default=5000.0, help="rows/second (default 5000)")
    c.add_argument("--min-rows", type=int, default=100)
    c.add_argument("--max-rows", type=int, default=1_000_000)
    r = sub.add_parser("run", help="run or resume a compiled .sql against a database")
    r.add_argument("sql")
    r.add_argument("--db", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "compile":
        try:
            with open(args.source, encoding="utf-8-sig") as f:   # tolerate a Windows BOM
                sql = compile_to_sql(f.read(), args.db, args.drop_prob, window=args.window,
                                     checkpoint_cost=args.checkpoint_cost, throughput=args.throughput,
                                     min_rows=args.min_rows, max_rows=args.max_rows)
        except CompileError as e:
            print(f"{args.source}: {e}", file=sys.stderr)
            return 2                                   # nothing written, DB untouched
        with open(args.output, "w", encoding="utf-8", newline="\n") as f:
            f.write(sql)
        print(f"wrote {args.output} ({sql.count('-- @block type=chunk')} chunks)")
        return 0

    kill = os.environ.get("CS_KILL_AFTER")
    hook = (lambda n: os._exit(137) if n >= int(kill) else None) if kill else None
    with open(args.sql, encoding="utf-8") as f:
        n = runner.run(args.db, f.read(), after_block=hook)
    print(f"executed {n} blocks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
