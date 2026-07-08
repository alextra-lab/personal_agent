# FRE-718 — ADR-0105 T6 — Postgres parameter tuning (RAM-aware) + auto-scheduled maintenance

**Ticket:** FRE-718 (Approved, `stream:build2`, `Tier-2:Sonnet`) — Linear Async Feedback Channel
**Backing ADR:** ADR-0105 (Accepted) D8 + AC-7. **Instance-wide infra change — build prepares the
config + code; master applies it with the required Postgres restart** (an always-ask-class deploy),
per the ticket's explicit instruction. This PR does **not** restart the live instance.

## 1. Verified-live facts (grounding the "not guessed" requirement)

Read-only inspection of the live `cloud-sim-postgres` container (no writes, no config changes):

- `docker inspect cloud-sim-postgres`: `mem_limit = 536870912` bytes = **exactly 512MB** — matches
  `docker-compose.cloud.yml:42`'s `mem_limit: 512m`. This is Postgres's **actual** share of the
  ~10GiB-available host (Neo4j gets 1536MB, ES 2048MB, etc.) — not a guess.
- Live `SHOW` confirms the ADR's stated stock defaults exactly: `shared_buffers=128MB`,
  `work_mem=4MB`, `random_page_cost=4`, `effective_io_concurrency=1`, `track_io_timing=off`,
  `effective_cache_size=4GB` (**8× the entire container's memory limit** — the nonsensical value the
  ADR calls "an optimistic effective-cache size for a shared host"), `max_connections=100`,
  `autovacuum=on`, PG17.9.
- `pg_database_size('personal_agent')` = **20MB** total — a genuinely small store today.
- `sysgraph` has 10 tables, all owned by `sysgraph_role` (so that role can `VACUUM`/`ANALYZE` its own
  tables with no extra grant). `pg_stat_user_tables` for all 10 shows `last_autovacuum`/`last_analyze`
  = NULL (never run) — expected at this volume (`proposal` has 6 live / 1 dead rows, nowhere near the
  stock 20% `autovacuum_vacuum_scale_factor` threshold). This is exactly the "low steady-state volume
  can otherwise leave autovacuum legitimately idle" case AC-7 names.
- No `vector`/embedding column exists on any `sysgraph` table today (confirmed via
  `information_schema.columns`), though the `vector` extension (0.8.2) is installed globally. The
  ticket's "vector-index maintenance cadence if the embedding column is enabled" is therefore **N/A
  today** — handled as a documented conditional, not fabricated dead code for a column that doesn't
  exist.
- `shared_preload_libraries` is empty and `pg_extension` has no `pg_stat_statements` row — the
  standard query-performance-auditing extension is **not enabled**. Enabling it requires
  `shared_preload_libraries='pg_stat_statements'`, which (unlike a plain `-c` runtime setting) can only
  take effect at server **start** — so it must ride the same restart this ticket already requires, or
  wait for a second one.
- **CPU throttling, from cgroup `cpu.stat` (the real evidence, not a point-in-time `docker stats`
  snapshot — cumulative since the container's 2026-06-17 start, ~21 days of uptime):** postgres has
  been throttled in **6.90%** of scheduling periods (`nr_throttled=66112` / `nr_periods=958322`), and
  lost **~19.3%** as much time to throttling as it spent actually computing
  (`throttled_usec=2,594,636,593` vs `usage_usec=13,473,440,749`). Checked against peer containers for
  context: neo4j (1.0 CPU) shows a 6.9% *time*-ratio but only 0.10% of *periods* throttled (rare,
  chunky stalls); elasticsearch (1.0 CPU) shows 1.1% time-ratio, 0.18% periods (comparatively
  unaffected); gateway (1.0 CPU) 1.0%/0.06%; reranker (4.0 CPU) a high 5.3% period-rate but a
  negligible 0.06% time-ratio (frequent but very brief stalls). **Postgres at 0.5 CPU is the clear
  outlier** — both the highest period-throttle rate and, by a wide margin, the highest time-cost
  ratio — a persistent, low-grade CPU tax rather than an occasional stall. This is concrete,
  owner-reviewed evidence (not a guess) that 0.5 CPU is constraining it.

## 2. Design — three independent parts

### 2a. Postgres parameter tuning — `docker-compose.cloud.yml` `command:` override + a CPU bump

Add a `command:` array to the `postgres` service (the standard postgres Docker image's supported
`-c key=value` override mechanism; `pgvector/pgvector:pg17` doesn't change the entrypoint —confirmed
via `docker inspect`: `Entrypoint=["docker-entrypoint.sh"]`, `Cmd=["postgres"]`).

**CPU allocation — `cpus: 0.5` → `cpus: 1.0`, backed by the §1 throttling evidence, not a guess.**
Matches gateway/neo4j/elasticsearch's tier. Host-wide allocation goes from 9.5 to 10.0 against 8
physical cores (a normal, already-established over-commit pattern for bursty containers — reranker
alone already claims 4.0). This is a judgment call the owner made explicitly after reviewing the
throttling numbers, not a default I chose unilaterally.

**CPU-aware parallelism (a correctness fix uncovered by checking the CPU side, not named in the ADR's
own parameter list but squarely inside "make Postgres operationally ready for its actual resource
envelope"):** `max_parallel_workers_per_gather=0`. Postgres auto-detects **8** CPUs at startup because
Docker's CPU quota doesn't hide the host's core count from `/proc/cpuinfo` (no `cpuset` is configured)
— so it sizes `max_parallel_workers_per_gather=2` for 8 phantom cores it never actually gets scheduled
on. Even after the bump to 1.0 CPU, that's still a fraction of a core; spawning parallel workers under
that quota adds context-switch overhead for no real speedup (and this workload is shallow-traversal /
small-table per ADR-0105's own framing, so parallel scans were never going to help it regardless of
CPU headroom). `max_worker_processes`/`max_parallel_workers` (the background-worker pool *ceilings*,
also used by extensions unrelated to query parallelism) are left untouched — only per-gather
parallelism, the setting that actually spawns workers for a query, needs zeroing.

**`pg_stat_statements` — enabled, riding this same restart.** Not in the ADR's named parameter list,
but it is the standard tool for "make Postgres operationally ready to audit," directly serves AC-7's
own framing, and `shared_preload_libraries` can only take effect at server start — deferring this to a
later ticket would mean a second unnecessary restart of a live shared instance for something that
costs nothing to fold into this one (build-skill Step 5: a supporting change needed to meet this
ticket's actual objective, not separate scope). Two-part change: `shared_preload_libraries` in the
`command:` override (below), plus `CREATE EXTENSION IF NOT EXISTS pg_stat_statements;` — a one-time DDL
statement master runs once after the restart (documented in the PR/Linear comment as an exact command,
same posture as the `SHOW` verification commands for the other tuned values — build cannot run DDL
against the live instance itself).

**SSD-correctness (ADR-specified exact values, memory-neutral) + the two additions above:**
```yaml
command:
  - postgres
  - -c
  - random_page_cost=1.1
  - -c
  - effective_io_concurrency=200
  - -c
  - track_io_timing=on
  - -c
  - max_parallel_workers_per_gather=0
  - -c
  - shared_preload_libraries=pg_stat_statements
```

**RAM-aware sizing, computed against the verified 512MB container limit — not the default that
assumes a dedicated box (standard tuning ratio: shared_buffers ~25% of the pool available to
Postgres, effective_cache_size ~75% reflecting the OS-cache ceiling *within the same 512MB cgroup*):**
```yaml
  - -c
  - shared_buffers=128MB           # 25% of 512MB — already the value in place; made EXPLICIT/intentional
  - -c                             # rather than an accidental match with the stock default, per the
  - effective_cache_size=384MB     # ADR's "right-size... not the 4GB planner default" instruction
```
`effective_cache_size=384MB` (75% of 512MB) replaces the nonsensical stock 4GB (8× the container's
entire memory ceiling) with the actual honest ceiling — the single highest-value fix here, since a
wildly-wrong effective_cache_size skews the planner's index-vs-seqscan cost estimates.
`mem_limit` itself stays at `512m` — the throttling evidence in §1 is CPU-specific
(`cpu.stat`); live memory usage (68MiB/512MiB, 13%) shows no comparable RAM pressure signal, so only
`cpus` moves.

**`work_mem` is deliberately left at the stock 4MB — no bump.** Codex plan-review flagged this as a
load-bearing OOM-risk finding: `work_mem` is per-sort-operation-**per-connection**, and
`max_connections=100` means a worst-case `work_mem=8MB` bump could demand up to 800MB if many
connections sort concurrently — 1.5× the container's entire 512MB `mem_limit`, on a **live production
database** where an OOM-kill is a real incident, not a bug to fix later. The ADR names this as a
"small"/"modest" nice-to-have for recursive-CTE sorts, not a hard requirement, and the current
workload is small enough (20MB db, depth-capped 2-3-hop traversals) that the stock 4MB is very likely
already adequate. Lowering `max_connections` in tandem (the standard companion move for safely raising
`work_mem` on a memory-constrained host) is a plausible future mitigation, but it's an
instance-wide behavior change none of this ticket's callers asked for and outside its
verified-safe scope — deferred, not silently dropped: noted as a follow-up consideration in the PR/
Linear comment, not filed as a ticket (a judgment call to revisit only if `work_mem` pressure is
actually observed).

**Not applied to `docker-compose.yml` (local dev)** — that instance has no `mem_limit`/host-RAM/CPU
over-commit constraint the ADR is solving for and no throttling evidence was (or could be) collected
against it; the ticket's "the shared instance" and ADR's "the prod host is RAM-binding" both scope this
to the cloud/production compose file. Touching local dev config beyond what the ticket asks risks an
unrelated behavior change for other developers' environments.

**Net `docker-compose.cloud.yml` `postgres` service diff (illustrative):**
```diff
-    mem_limit: 512m
-    cpus: 0.5
+    command:
+      - postgres
+      - -c
+      - random_page_cost=1.1
+      - -c
+      - effective_io_concurrency=200
+      - -c
+      - track_io_timing=on
+      - -c
+      - max_parallel_workers_per_gather=0
+      - -c
+      - shared_preload_libraries=pg_stat_statements
+      - -c
+      - shared_buffers=128MB
+      - -c
+      - effective_cache_size=384MB
+    mem_limit: 512m
+    cpus: 1.0
```

### 2b. Scheduled `VACUUM (ANALYZE)` job for `sysgraph` — mirrors the FRE-717/D7 `outcome_ingestion.py` pattern exactly

**New method on `SysgraphRepository`** (`src/personal_agent/sysgraph/repository.py`) — the class's own
docstring states it is "the only code path permitted to open a connection to the sysgraph schema", so
maintenance goes through it rather than a bypass raw-pool call from the job:

```python
async def list_table_names(self) -> list[str]:
    """Return every table name in the sysgraph schema (pg_tables, not hardcoded — survives
    future migrations adding tables without this job needing an update)."""

async def vacuum_analyze_table(self, table_name: str) -> None:
    """Run VACUUM (ANALYZE) on one sysgraph table. Must not be called inside an explicit
    asyncpg transaction block (VACUUM cannot run in a transaction) -- conn.execute() outside
    a `conn.transaction()` context satisfies this."""

async def vacuum_analyze_all(self) -> dict[str, str]:
    """Run vacuum_analyze_table for every sysgraph table; returns {table_name: "ok"|"<error>"}
    so one failing table doesn't abort the rest, mirroring outcome_ingestion's
    per-item-try/except-continue shape."""

async def record_maintenance_run(self, results: dict[str, str]) -> None:
    """Insert a row into sysgraph.stat (name='sysgraph_maintenance_run', value=<successful
    table count>, metadata={"results": results}, observed_at=NOW()) -- an existing, currently-
    unused-by-D7 table (name/value/metadata/observed_at, migration 0014) reused as the durable,
    SQL-queryable "last succeeded" signal AC-7 asks for (`SELECT * FROM sysgraph.stat WHERE
    name='sysgraph_maintenance_run' ORDER BY observed_at DESC LIMIT 1`), so master's live
    verification is one query, not a log grep."""
```

**Lock/timeout characteristics, addressed explicitly rather than glossed over by the outcome_ingestion
mirror (codex plan-review point 6):** a plain `VACUUM` (no `FULL`) takes only a `SHARE UPDATE
EXCLUSIVE` lock, which does **not** block normal reads/writes on the table — no contention risk with
the app's own traffic. The repository pool's `command_timeout=10` (`repository.py:277`) is safe at
today's data volume (a 20MB database vacuums in well under a second) but is noted here as a known,
currently-inapplicable scaling limit — not silently ignored — for whoever revisits this once
`sysgraph` holds meaningfully more data.

**Future-table governance (codex plan-review point 7), a deliberate scope decision, not a gap:**
`list_table_names()` queries `pg_tables` scoped to `schemaname='sysgraph'` only, so it can never touch
another schema or a view. Whether a *future* sysgraph table should be excluded from this cadence is a
decision that belongs to whatever migration adds that table (e.g. a naming convention or an explicit
exclusion list added *then*, against a real requirement) — building exclusion machinery now for a
table that doesn't exist yet would be speculative scope beyond what this ticket asks.

**New job** `src/personal_agent/brainstem/jobs/sysgraph_maintenance.py`:

```python
async def run_sysgraph_maintenance(trace_id: str) -> None:
    """ADR-0105 D8/AC-7. Connects its own SysgraphRepository (mirrors run_outcome_ingestion),
    runs vacuum_analyze_all(), records the durable sysgraph.stat completion marker via
    record_maintenance_run(), logs a structured sysgraph_maintenance_completed summary
    (per-table results + counts), disconnects in finally. No vector-index maintenance step is
    emitted -- structurally checks list_table_names()-adjacent column metadata for a vector
    column and logs sysgraph_maintenance_vector_index_skipped_no_column if none exists (N/A
    today, not dead code for a hypothetical layout -- see §1)."""
```

**Wiring in `brainstem/scheduler.py`** — new daily-hour block in `_lifecycle_loop`, same shape as the
adjacent D7 outcome-ingestion block (`:745-770`):
```python
if (
    getattr(settings, "sysgraph_maintenance_enabled", True)
    and now.hour == self.sysgraph_maintenance_hour_utc
    and (self._last_sysgraph_maintenance_date is None or self._last_sysgraph_maintenance_date != today)
):
    try:
        from personal_agent.brainstem.jobs.sysgraph_maintenance import run_sysgraph_maintenance
        await run_sysgraph_maintenance(trace_id=iteration_trace_id)
        self._last_sysgraph_maintenance_date = today
    except Exception as exc:
        log.warning("sysgraph_maintenance_failed", error=str(exc), exc_info=True, trace_id=iteration_trace_id)
```
`self.sysgraph_maintenance_hour_utc = settings.sysgraph_maintenance_hour_utc` added alongside the
other `__init__` hour assignments; `self._last_sysgraph_maintenance_date` initialized `None` alongside
the sibling `_last_*_date` trackers.

**New settings** (`config/settings.py`, next to the D7 outcome-ingestion block):
```python
sysgraph_maintenance_enabled: bool = Field(default=True, description="Enable daily VACUUM (ANALYZE) of sysgraph tables (ADR-0105 D8/AC-7)")
sysgraph_maintenance_hour_utc: int = Field(default=9, ge=0, le=23, description="UTC hour for the daily sysgraph maintenance sweep (distinct from feedback polling's 7, outcome ingestion's 8)")
```

## 3. Tests (TDD)

- `tests/personal_agent/sysgraph/test_repository.py` (extend, **real test Postgres** — this schema's
  established convention, not mocks): `list_table_names()` returns all 10 known sysgraph tables;
  `vacuum_analyze_table("proposal")` succeeds with no exception; `vacuum_analyze_all()` returns
  `{table: "ok"}` for every table; a nonexistent table name in `vacuum_analyze_all`'s internal loop is
  caught and reported as an error string for that key without aborting the rest (seed via a fake
  extra name, or test `vacuum_analyze_table` directly raising on a bad name); `record_maintenance_run`
  inserts a row into `sysgraph.stat` with `name='sysgraph_maintenance_run'` that a follow-up `SELECT`
  finds — the AC-7 "last succeeded" evidence contract, proven at the repository level against a real
  Postgres, not just asserted as a log line.
- `tests/test_brainstem/test_sysgraph_maintenance.py` (new, mocked repository — mirrors
  `test_outcome_ingestion.py`'s `monkeypatch.setattr("personal_agent.sysgraph.SysgraphRepository", ...)`
  pattern exactly): disabled-flag skip; happy path calls `connect()` → `vacuum_analyze_all()` →
  `disconnect()` in order (disconnect always called, including on an exception from
  `vacuum_analyze_all`, via `finally`); a connect failure is caught and logged, not raised.
- `tests/test_brainstem/test_scheduler.py` (existing file — extend if a suitable pattern exists,
  else a focused new test): the daily-hour gate fires `run_sysgraph_maintenance` once per UTC day at
  `sysgraph_maintenance_hour_utc`, not on other hours, mirroring however the existing
  outcome-ingestion/feedback-polling hour-gate tests are structured (read the existing test first to
  match its exact fixture/mocking shape rather than inventing a new one).
- **No test touches the live `cloud-sim-postgres` container** — the repository-level tests run against
  the FRE-375-isolated test Postgres stack (`:5433`), same as every other sysgraph test.

## 4. Acceptance-criteria mapping (AC-7)

- *"The deployed instance reports the tuned values, not the stock defaults"* — this PR delivers the
  `command:` override (SSD costs, right-sized `effective_cache_size`, `max_parallel_workers_per_gather`,
  `shared_preload_libraries`, and the `cpus: 0.5 → 1.0` bump); **master proves this live** post-restart
  (`SHOW random_page_cost` / `effective_cache_size` / `max_parallel_workers_per_gather`, plus
  `docker inspect cloud-sim-postgres` for the CPU allocation, plus
  `CREATE EXTENSION IF NOT EXISTS pg_stat_statements;` then a `SELECT` against it) — build cannot
  restart the shared instance itself. The PR/Linear comment states the exact commands + expected values
  for master to run.
- *"Auto-maintenance is demonstrably running... either observed autovacuum activity... or proof the
  scheduled job last succeeded"* — this PR delivers the scheduled job + its tests (repository-level:
  proves `VACUUM (ANALYZE)` genuinely executes against a real Postgres; job-level: proves the
  connect→run→disconnect contract and the disabled/failure paths) **and** a durable, SQL-queryable
  completion marker (`sysgraph.stat` row via `record_maintenance_run`) so "last succeeded" is a single
  `SELECT`, not a log grep. **Master verifies "last succeeded" live** once deployed and the scheduler
  has run at least once (a live-instance observation, not something a build-session unit test can
  assert) — the PR/Linear comment states the exact query.

## 5. Quality gates

`make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` ·
`pre-commit run --all-files`. Self-review (Step 8): `code-review` at `high` effort (touches
production infra config + new src/ DB-access code); `security-review` (subprocess-adjacent? no —
touches DB access + deploy config, run it given the DB-connection surface).

## 6. Files touched

- `docker-compose.cloud.yml` (postgres service `command:` override + `cpus: 0.5 → 1.0`; update the
  file's header resource-limit comments to match)
- `src/personal_agent/sysgraph/repository.py` (add `list_table_names`, `vacuum_analyze_table`,
  `vacuum_analyze_all`, `record_maintenance_run`)
- `src/personal_agent/brainstem/jobs/sysgraph_maintenance.py` (new)
- `src/personal_agent/brainstem/scheduler.py` (new daily-hour block + `__init__` wiring)
- `src/personal_agent/config/settings.py` (2 new fields)
- `tests/personal_agent/sysgraph/test_repository.py` (extend)
- `tests/test_brainstem/test_sysgraph_maintenance.py` (new)
- `tests/test_brainstem/test_scheduler.py` (extend, matching existing hour-gate test pattern)
