# ChunkSafe

A compiler for **interruption-safe database reorganization** on mobile edge databases (SQLite).

Mobile devices lose connectivity or get killed mid-task. ChunkSafe takes a tiny language for reorganization jobs and compiles it into **chunked, retry-safe SQL** that can be stopped at any point and resumed without corrupting data.

```
REINDEX orders ON (region, ts);
REPARTITION trips BY zone;
UPDATE stats SET views = visits * 2;
UPDATE stats SET visits = visits + 1;   -- rejected at compile time: reads and writes the same column
```

## How it works
1. **Parse** the `.reorg` source (Lark) and check tables and columns against the target SQLite schema.
2. **Retry-safety pass:** Read/Write-set analysis rejects non-idempotent tasks before the database is touched.
3. **Chunk sizing:** chunk size comes from the predicted drop probability using Young's formula, `T = sqrt(2·C·D)`. A higher drop chance gives smaller chunks.
4. **Codegen:** each chunk is one transaction that does `INSERT OR REPLACE` into a shadow table and records progress. When all chunks are done, one atomic transaction swaps the shadow in (DROP, RENAME, recreate indexes).
5. **Runner:** resumes from the last committed chunk.

See [design.md](design.md) for the full design and [project_pipeline.md](project_pipeline.md) for the roadmap.

## Status
| Phase | Scope | State |
|---|---|---|
| 0 | Design docs, project skeleton | Done |
| 1 | Parser, semantic checks, IR, retry-safety pass | Done |
| 2 | Chunk sizing, SQLite codegen, CLI, resume runner, interruption simulation | Done |

40 tests pass, including drops at every block boundary, a crash just before COMMIT, a chunk run twice, and a real process kill followed by resume.

**Known limits:** `REPARTITION` builds a key index and does not physically re-cluster rows. `WITHOUT ROWID` tables and UPDATEs of key or unique columns are rejected. Chunk ranges are computed at compile time, so the app should not write to the table during a reorg.

## Quick start
```
pip install -r requirements.txt
python -m pytest

python -m compiler.cli compile examples/reindex.reorg --db app.sqlite --drop-prob 0.3 -o job.sql
python -m compiler.cli run job.sql --db app.sqlite     # re-run the same command to resume after an interruption
```
`compile` exits with code 2 and writes nothing if the program is unsafe or invalid. Other options: `--window`, `--checkpoint-cost`, `--throughput`, `--min-rows`, `--max-rows`. Setting the environment variable `CS_KILL_AFTER=n` makes `run` die after `n` blocks, which simulates a connectivity drop.

## Layout
```
compiler/        grammar.lark, parser, semantic, ir, passes/{safety,sizing}, dbinfo, codegen, runner, cli
tests/           parser, semantic, safety, sizing, codegen, interruption tests
examples/        sample .reorg programs (including one that is rejected)
```
