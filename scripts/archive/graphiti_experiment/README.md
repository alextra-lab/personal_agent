# Graphiti Experiment Archive (EVAL-02 / FRE-147)

> **STATUS: EXPERIMENT CLOSED — FROZEN HISTORICAL CODE**
>
> This directory is an archive of the Graphiti vs Seshat comparison experiment
> run in March 2026. The experiment is complete; this code is **not maintained**
> and **not a supported entrypoint**.

## What is here

| Path | Description |
|------|-------------|
| `graphiti_experiment.py` | CLI entry point that orchestrated the comparison runs |
| `experiment/config.py` | LLM and experiment configuration dataclasses |
| `experiment/graphiti_runner.py` | Scenario runners against Graphiti (second Neo4j) |
| `experiment/seshat_runner.py` | Scenario runners against the Seshat memory service |
| `experiment/data_loader.py` | Synthetic episode generation + real telemetry loading |
| `experiment/metrics.py` | Timing, precision/recall, dedup metrics |
| `experiment/report.py` | JSON + markdown report generation |

Archived test helpers live at: `tests/archive/graphiti_experiment/`

## Why this code no longer works out of the box

1. The **ephemeral second Neo4j** (`neo4j-experiment`, port `7688`) has been removed
   from `docker-compose.yml`. The experiment used a separate isolated database so
   Graphiti's graph data could not contaminate the primary Seshat Neo4j.
2. The `graphiti-core` package may not be installed in the current environment.
3. Internal imports assume the old `scripts/` layout (e.g. `from experiment.config import ...`).

## Results

See `docs/research/GRAPHITI_EXPERIMENT_REPORT.md` for findings and conclusions.
See `docs/plans/completed/2026-03-28-eval-02-graphiti-experiment.md` for the full
experiment plan (superseded setup; conclusions unchanged).
