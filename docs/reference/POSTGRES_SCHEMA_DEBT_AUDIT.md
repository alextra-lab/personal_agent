# Postgres Schema-Debt Audit — Residue Sweep (FRE-597)

> Source finding: `docs/superpowers/plans/the-following-information-comes-logical-pie.md` §6
> (A2 / DB-1 remainder, follow-on to FRE-591). Column-level definitions live in
> [`SCHEMA_REFERENCE.md`](../guides/SCHEMA_REFERENCE.md) §4 — this doc records the
> keep/drop/document decision per table, not the schema itself.

## `embeddings` — **DROP** (done)

Speculative pgvector table (`docker/postgres/init.sql`, comment: "for future semantic
search"), with an HNSW index, never read or written by any application code. No
SQLAlchemy model, no repository, no raw-SQL reference anywhere in `src/`. Semantic
search was built against Neo4j instead (`personal_agent.memory.embeddings`), bypassing
this table entirely.

Guaranteed empty in every environment (nothing has ever inserted a row), so dropping it
is cheap and safe. Removed from `docker/postgres/init.sql` (fresh installs) and dropped
for existing databases via `docker/postgres/migrations/0024_drop_dead_embeddings_table.sql`.
The `vector` extension itself stays enabled — `artifacts.embedding` still depends on it.

## `captains_log_captures` / `captains_log_reflections` — **KEEP**, document

Live Captain's Log data goes to **Elasticsearch**, not these Postgres tables
(`captains_log/capture.py:write_capture`, ES-only — no asyncpg call in that module).
No live write or read path in application logic. However the tables are not dead
weight to drop:

- `observability/joinability/walk.py` cross-references them (`SELECT trace_id FROM
  captains_log_captures|reflections WHERE trace_id = ANY(...)`) as part of the
  cross-substrate joinability integrity checker.
- `scripts/cleanup_eval_data.py` issues `DELETE FROM` both tables as part of eval-data
  purge (`AGENT_LINEAR_API_KEY`-gated maintenance tooling).

Decision: keep the schema for these two consumers; no migration needed. Note for
future readers: `telemetry/lifecycle.py` / `lifecycle_manager.py` / `brainstem/scheduler.py`
also use the strings `"captains_log_captures"` / `"captains_log_reflections"` as labels
for a **separate, file-based** JSON archive (`telemetry/captains_log/**/*.json`) — that
code has no Postgres/asyncpg dependency at all and is documented independently in
[`DATA_LIFECYCLE.md`](DATA_LIFECYCLE.md). Same names, two unrelated data stores — don't
conflate them when tracing either system.

## `metrics` — **KEEP**, document (write path not yet live)

Schema, SQLAlchemy model (`MetricModel`), and a full repository
(`service/repositories/metrics_repository.py`: `write`, `write_batch`, `query`,
`get_stats`) all exist, but `MetricsRepository` is never imported or instantiated
outside its own module — no live write path. Only other reference is a read from the
joinability diagnostic tool.

Decision: keep — the repository is built and ready, dropping it would discard working
code for no gain, and wiring it into the app is a feature decision (not schema debt)
that's out of this ticket's scope. Flagged here as a candidate for a future ticket if
that observability surface is wanted; not filed speculatively.

## `consolidation_attempts` — confirmed **live**, no action

Write path is real: `second_brain/attempts.py:record_consolidation_attempt` (raw
asyncpg, transactional sequence read + insert), called from four sites in
`second_brain/consolidator.py`. No debt here — included for completeness since the
ticket asked to confirm both `metrics` and `consolidation_attempts` together.

## `api_costs` / cost-gate tables (`budget_policies`, `budget_counters`,
`budget_reservations`) / `route_traces` — **KEEP**, document design intent (don't "fix")

All persist via **raw asyncpg**, deliberately **outside** `service/repositories/`
(which only holds `constraint_preferences_repository.py`, `metrics_repository.py`,
`session_model_selection_repository.py`, `session_repository.py`):

- `llm_client/cost_tracker.py` (`api_costs`)
- `observability/route_trace/ledger.py` (`route_traces`)
- `cost_gate/gate.py` (`budget_policies` / `budget_counters` / `budget_reservations`)
- `second_brain/attempts.py` (`consolidation_attempts`, same lane)

Each module's own docstring already documents the rationale: the hot path needs a
single transaction with `SELECT ... FOR UPDATE` locks and bulk `UPDATE`s, where the
SQLAlchemy ORM layer adds overhead without benefit, and (for `cost_tracker`/`gate`) an
identity boundary per ADR-0074. All four share a `_normalize_asyncpg_dsn` helper
(`cost_tracker.py`) — a deliberate, parallel "raw asyncpg lane" alongside the
SQLAlchemy repository lane, not an oversight. These are also the most actively
migrated tables in the schema (`0001`, `0002`, `0004`, `0008`, `0009`, `0010`, `0013`,
`0021`, `0022`), consistent with being live and evolving. No corrective change made —
recorded here per the ticket's "document, don't fix" instruction.
