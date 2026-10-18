# ChunkSafe — Implementation Pipeline

Sequential phases. Each phase ends with a **handoff contract** (what artifact, what guarantees) and an **exit gate** (tests that must pass before the next phase starts).

```
Phase 0 Docs+Skeleton → Phase 1 Frontend+Safety → Phase 2 Backend → Phase 3 Testing/Demo
```

## Phase 0 — Foundations (done)
- `design.md`, `project_pipeline.md`, project skeleton, `requirements.txt` (`lark`, `pytest`).
- **Exit gate:** user approval.

## Phase 1 — Frontend & Safety
| Step | Work | Files | Output |
|---|---|---|---|
| 1.1 | Lark grammar; parse 3 statements; case-insensitive keywords, `--` comments | `grammar.lark`, `parser.py`, `lexer.py`, `ast_nodes.py` | `Program` AST with line/col |
| 1.2 | Semantic checks against schema catalog: table exists, columns exist, no duplicate index cols, types for arithmetic | `semantic.py`, `errors.py` | Annotated AST or `SemanticError` |
| 1.3 | AST → IR; derive `reads`/`writes` per kind | `ir.py` | `list[Task]` (no chunks yet) |
| 1.4 | Retry-safety pass (Rules 1–4 of design §7) | `passes/safety.py` | Verified IR or `UnsafeRetryError` |

**Handoff 1 → 2:** a `list[Task]` where `reads ∩ writes = ∅` for every UPDATE, every table/column resolved against the catalog, tasks in source order with dependency edges. Nothing downstream re-validates safety.
**Exit gate:** `test_parser`, `test_semantic`, `test_safety` green; `unsafe_update.reorg` is rejected with a located message; `reindex.reorg`/`repartition.reorg` accepted.

## Phase 2 — Backend: Sizing & Codegen
| Step | Work | Files | Output |
|---|---|---|---|
| 2.1 | Sizing pass: `p → D → T_opt → chunk_rows → chunks` with clamps and `T < D/2` guard | `passes/sizing.py` | IR tasks with `chunks` + `ChunkPlan` |
| 2.2 | SQLite codegen: progress table, shadow DDL, per-chunk `INSERT OR REPLACE` blocks, atomic swap, index recreation | `codegen.py` | `.sql` + `manifest.json` |
| 2.3 | CLI: `chunksafe compile in.reorg --schema db.sqlite --drop-prob 0.3 -o out.sql` (+ `--checkpoint-cost`, `--throughput`, `--window`) | `cli.py` | Exit codes: 0 ok, 2 compile error |
| 2.4 | Resume runner used by demo/tests | `runner.py` | Skips `done` chunks, runs swap once |

**Handoff 2 → 3:** `out.sql` + `manifest.json`: ordered, labelled blocks (`prepare`, `chunk i`, `swap`), each independently transactional and idempotent; manifest lists blocks and the task/chunk each belongs to.
**Handoff inside Phase 2 (2.1 → 2.2):** codegen consumes only chunked IR; it must never compute sizes itself.
**Exit gate:** `test_sizing` (monotonicity, coverage) and `test_codegen` (golden SQL, executes without error on a fixture DB) green.

## Phase 3 — Testing & Demo
| Step | Work | Files |
|---|---|---|
| 3.1 | Interruption simulation: run N chunks, kill (exception / subprocess terminate), resume; compare to one-shot reference | `tests/test_interruption.py` |
| 3.2 | Kill at every chunk boundary and mid-transaction (rollback check); double-run a chunk; double-run the swap | same |
| 3.3 | Demo script + README: `p=0.1` vs `p=0.9` chunk counts, rejected `UPDATE` | `README.md`, `examples/` |

**Exit gate:** all tests green; data-integrity assertions hold at every kill point.

## Dependency Graph
```
grammar → parser → semantic → IR gen → safety ─(Handoff 1)→ sizing → codegen → CLI → runner → interruption tests
```

## Risks / Decisions to Confirm
1. Schema source for semantic checks: SQLite file (default) vs JSON catalog.
2. Writes to the live table during reorg: assumed locked out (out of scope).
3. Tables lacking an integer PK use `rowid` for chunk ranges.
