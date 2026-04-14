# tests/archive — Frozen Historical Tests

> **These tests are NOT collected by the default pytest run.**
>
> `pytest.ini_options.norecursedirs` explicitly excludes this directory so archived
> tests never run in CI or a plain `pytest` invocation. This is intentional — the
> code here is frozen historical reference only.

## Contents

| Directory | Experiment | Status |
|-----------|-----------|--------|
| `graphiti_experiment/` | EVAL-02 / FRE-147 (Graphiti vs Seshat Neo4j comparison) | Closed Mar 2026 |

## Why tests are here and not deleted

Git history is enough for code, but having the test bodies visible makes it easy
to understand *what was measured* when reading the companion report at
`docs/research/GRAPHITI_EXPERIMENT_REPORT.md`.

## If you need to run them

These tests require:
- A running Neo4j instance on `bolt://localhost:7687` (or a second instance for the experiment variant)
- The `graphiti-core` package installed (not in default dev dependencies)
- The `neo4j_checker` module restored to an active harness path

See `scripts/archive/graphiti_experiment/README.md` for the full setup context.
