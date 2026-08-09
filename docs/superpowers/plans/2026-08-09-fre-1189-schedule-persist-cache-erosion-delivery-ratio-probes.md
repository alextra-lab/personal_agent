# FRE-1189 — Schedule and persist the cache-erosion and delivery-ratio probes

ADR-0134 T3 (D4 rule 2 prerequisite). Ticket: https://linear.app/frenchforest/issue/FRE-1189

## Scope (from the ticket)

Two of four instruments (cache-erosion monitor, delivery-ratio probe) are manual CLIs that persist
nothing, so ADR-0134 rule 2 ("a scheduled probe stops writing its result document") cannot watch them
and AC-7's false-negative cross-check has no data. Give both a scheduled run under `BrainstemScheduler`
and a persisted ES result document, following the joinability/SLM-health pattern exactly. **Do not
touch either probe's verdict logic** (`cache_erosion/monitor.py`, `delivery_ratio/probe.py`,
`delivery_ratio/collect.py`) — execution and persistence only.

Blocked-by FRE-1008 is merged into `main` (PR #879, commit `50fb4493`) — unblocked.

## Acceptance criteria (this ticket's own — verbatim from the ticket)

- **AC-1** — both probes run with no manual invocation (compressed-interval integration test against
  the real `BrainstemScheduler`).
- **AC-2** — each run's result document carries its verdict (per-family for delivery-ratio including
  `unverifiable`; the computed result for cache-erosion), not merely that the probe ran.
- **AC-3** — the interval is settings-driven, not hardcoded, and honoured (two consecutive runs under
  the compressed-interval harness are spaced by the configured value).
- **AC-4** — delivery-ratio still writes nothing to production beyond its own result doc.
- **AC-5** — both targets (ES, Postgres) come from settings, no hardcoded URI.

## Existing pattern being mirrored (joinability / SLM-health — read, not modified)

- `brainstem/scheduler.py` `__init__` — three lines per probe: `_last_<probe>_run`,
  `<probe>_enabled` / `<probe>_interval_seconds` via `getattr(settings, ..., default)`.
- `brainstem/scheduler.py` `_lifecycle_loop()` (~line 1075 onward) — a gated block per probe: `if
  enabled and (last_run is None or elapsed >= interval): try: lazy-import + await
  run_scheduled_<probe>(es_client=cast("AsyncElasticsearch | None", self._lifecycle_es_client));
  last_run = now; except Exception: log.warning(...)` — never raises, so one probe's failure cannot
  kill the loop.
- `observability/<probe>/scheduler_runner.py` — thin wrapper: reads settings, runs the probe, writes
  via the sink, swallows write errors.
- `observability/<probe>/sink.py` — `index_name_for(doc, *, prefix)` → `f"{prefix}-{ts:%Y-%m}"`
  (monthly, FRE-543/FRE-1036), `write_result(es, doc, *, prefix)` → `es.index(index=..., id=uuid4(),
  document=doc.model_dump(mode="json"))`, logs `<probe>_result_indexed`.
- Result doc — frozen Pydantic `BaseModel`, `kind: str = "system:<probe>_probe"` sentinel.
- `docker/elasticsearch/monitors-<probe>-index-template.json` + `-ilm-policy.json`, registered by
  explicit `put_resource`/`put_and_apply_template` calls in `scripts/setup-elasticsearch.sh` (not
  glob-discovered).

## Design decisions

Revised after codex plan-review (2026-08-09) — see "Codex review findings addressed" below for the
specific defects that drove each change from the first draft.

1. **Cache-erosion has one substrate (ES only).** Unlike joinability (which partially walks other
   substrates when ES is absent), cache-erosion's only query target is `agent-logs-*`. When
   `es_client is None`, `run_scheduled_cache_erosion_probe` logs and returns `None` — there is nothing
   to compute without it. Codex confirmed this is correct.
2. **Delivery-ratio needs ES + Postgres. Use a single short-lived `asyncpg` connection, not a pool.**
   The scheduler only ever hands probes an ES client (`self._lifecycle_es_client`); Postgres is not
   otherwise owned by the scheduler. `collect_report()` issues one sequential Postgres count per run —
   a 1-2 connection pool opened and torn down once a day adds setup/teardown cost with no reuse or
   concurrency benefit, so use `asyncpg.connect(...)` directly (matching the CLI's own
   `delivery_ratio_monitor.py` precedent), not `asyncpg.create_pool(...)` (joinability's heavier
   pattern, justified there by multiple concurrent substrate walks). Build the DSN via
   `llm_client.cost_tracker._normalize_asyncpg_dsn` — the same helper the CLI already uses for this
   exact connection. Pass a `timeout=10.0` to `asyncpg.connect(...)` so a wedged Postgres cannot hang
   this scheduler's single serial lifecycle loop indefinitely. Close via a `_close_pg_conn()` helper
   that catches and logs a close failure rather than a bare `await conn.close()` in `finally` — an
   unguarded close can raise and overwrite an otherwise-successful return (the same reason
   `joinability/scheduler_runner.py._close()` wraps each resource's close in its own try/except).
3. **Window for scheduled delivery-ratio runs: yesterday's full UTC day** (`since = until = today -
   1`), matching the CLI's own default window. Window size is not a criterion; only the *interval*
   between runs must be settings-driven (AC-3) — so this is fixed, not newly configurable. Codex
   confirmed this reading of AC-3.
4. **Cache-erosion interval default: hourly (3600s)** — the probe's own module docstring already
   states "Designed to run from the brainstem scheduler (hourly)"; matches joinability's default.
   **Delivery-ratio interval default: daily (86400s)** — the measured window only finalizes once a
   day; re-running more often than that just re-measures the same window.
5. **Result-doc shaping lives in each probe's `result.py`**, not `scheduler_runner.py` — mirrors
   joinability's `result.py` (which also hosts `substrate_docs_from_result`). Cache-erosion's
   `CallsiteResult.hashes_a/b` are `frozenset[str]`, not JSON-serialisable — the doc model stores
   `hash_count_a/b` (`len()`) instead of the raw sets (codex: acceptable deliberate projection — AC-2
   is satisfied by `results[].status`/`jaccard`/counts/`any_eroded`; raw hash evidence stays queryable
   from the underlying log documents). Delivery-ratio's `FamilyDeliveryRecord` is built via an
   **explicit field-by-field adapter** (`family=f.family, oracle=f.oracle, ...`), not
   `FamilyDeliveryRecord(**f.to_dict())` — codex flagged the `**kwargs` form as drift-prone: a new
   `to_dict()` key would be silently dropped by Pydantic's default extra-field handling rather than
   failing loudly, and a persistence-schema change should be a deliberate, visible diff line.
6. **`run_at` is captured once per run and reused**, not re-derived from separate `datetime.now()`
   calls, so a run straddling UTC midnight cannot produce an internally inconsistent document (codex
   finding 6): `run_at = datetime.now(timezone.utc)` then `yesterday = run_at.date() -
   timedelta(days=1)` in delivery-ratio's runner. Cache-erosion's result doc uses
   `report.computed_at` — the canonical timestamp the unchanged probe already produces — as `run_at`,
   not a second `datetime.now()` call in the runner.
7. **The scheduler advances `_last_<probe>_run` only when the runner returns a non-`None` doc**, not
   unconditionally after `await`ing it (codex finding 2 — a real defect in the first draft, not a style
   preference). Both runners return `None` on a recoverable condition (disabled, no ES client, Postgres
   unreachable) that is not an exception — the existing joinability/SLM-health precedent never hits this
   case (their runners always return a doc or raise), so this ticket's runners are the first place the
   scheduler needs to check the return value rather than just catching exceptions. A transient Postgres
   outage must not suppress the next delivery-ratio attempt for a full 24h interval.
8. **No `source` field on either new result doc** — unlike joinability's `ResultDoc` (written by both
   CLI and scheduler), neither `cache_erosion_monitor.py` nor `delivery_ratio_monitor.py`'s CLI writes
   to ES today and this ticket does not change that (out of scope) — there is only one writer
   (scheduler), so `source` would be a constant with nothing to distinguish. Matches
   `SlmHealthSnapshot`'s simpler precedent (no `source` field either).
9. **ES templates + ILM policies are folded in**, not a separate ticket — every existing scheduled
   probe has one, dynamic-mapping fallback would misclassify `day_a`/`day_b`/`since`/`until` as text,
   and it is ~30 lines of boilerplate copied from the SLM-health template. This is a supporting change
   needed to make the sink pattern actually match its precedent, not new scope (build skill § 5). Codex
   confirmed this is reasonable scope, not overreach. Both `results` (cache-erosion) and `families`
   (delivery-ratio) array fields map as `nested`, not plain `object` — a plain object array lets a query
   match `family=X` from one array element and `status=breach` from a different one; `nested` keeps each
   element's fields joined for correct per-element filtering (e.g. "the api_cost_recorded family
   breached", not "some family is api_cost_recorded AND some family breached").

### Codex review findings addressed

First-draft plan-review (background job, cancelled after 12 min of no log progress — a genuine hang,
confirmed via `codex-companion.mjs status --json`'s `updatedAt` timestamp not advancing; retried fresh
and completed in ~4 min). Findings folded into the design above and the file-by-file sections below:

- **High — sleep-stub self-recursion.** The compressed-interval test must capture `real_sleep =
  asyncio.sleep` *before* patching `personal_agent.brainstem.scheduler.asyncio.sleep`, and the
  replacement must call `real_sleep(...)`, not `asyncio.sleep(...)` — both names resolve to the same
  patched target, so calling the unqualified name from inside its own replacement recurses forever.
- **High — timestamp advances even when the runner returns `None`.** Addressed as design decision 7
  above; scheduler.py block updated accordingly (see Files to modify).
- **High — the AC-3 test didn't prove "settings-driven".** Overriding
  `scheduler.<probe>_interval_seconds` post-construction only proves the lifecycle gate honours an
  instance attribute, not that it came from settings. Added a separate, non-integration constructor
  test per new settings field (mirrors `test_domain_guard_warm_interval_derived_from_ttl`'s existing
  pattern of constructing `BrainstemScheduler()` under a patched `settings` module and asserting the
  instance attribute matches).
- **Medium-high — unguarded `finally: await pg_pool.close()`.** Addressed via `_close_pg_conn()` in
  design decision 2.
- **Medium — pool vs. connection.** Addressed in design decision 2 (switched to `asyncpg.connect`).
- Medium findings on time-capture-once, the `results.threshold` template field, and the `**to_dict()`
  adapter are folded into decisions 5, 6, and the ES template section below.

## Files to create

1. `src/personal_agent/observability/cache_erosion/result.py`
   - `CallsiteErosionRecord` (frozen `BaseModel`): `callsite`, `day_a`, `day_b`, `hash_count_a: int`,
     `hash_count_b: int`, `jaccard: float`, `status: Literal["stable","eroded","insufficient_data"]`,
     `threshold: float`.
   - `CacheErosionResultDoc` (frozen `BaseModel`): `run_at: datetime`, `window_days: int`,
     `threshold: float`, `any_eroded: bool`, `results: list[CallsiteErosionRecord]`, `trace_id: str`,
     `kind: str = "system:cache_erosion_probe"`.
   - `from_report(report: ErosionReport, *, window_days: int, trace_id: str) -> CacheErosionResultDoc`
     — the ES-shaping adapter (converts `frozenset` → `len()`). `run_at=report.computed_at` (the
     probe's own canonical timestamp — no separate `datetime.now()` call in the runner; codex finding
     6).

2. `src/personal_agent/observability/cache_erosion/sink.py`
   - `index_name_for(doc, *, prefix) -> str` — `f"{prefix}-{doc.run_at:%Y-%m}"`.
   - `write_result(es, doc, *, prefix) -> None` — fresh-UUID doc id, `es.index(...)`, logs
     `cache_erosion_result_indexed`. Mirrors `slm_health/sink.py` verbatim.

3. `src/personal_agent/observability/cache_erosion/scheduler_runner.py`
   - `run_scheduled_cache_erosion_probe(*, es_client: "AsyncElasticsearch | None") ->
     CacheErosionResultDoc | None`. `None` early-return when disabled or `es_client is None`.
     Otherwise: `compute_erosion_report(es_client, logs_prefix=settings.elasticsearch_index_prefix,
     window_days=settings.cache_erosion_probe_window_days,
     threshold=settings.cache_erosion_probe_threshold)` → `from_report(report,
     window_days=window_days, trace_id=ctx.trace_id)` → `write_result(...)` inside its own
     `try/except` (write failure logged, not raised — matches SLM-health). Returns the doc
     regardless of whether the ES write succeeded — only a `None` return means "did not run" (design
     decision 7: the scheduler advances its timestamp on any non-`None` return).

4. `src/personal_agent/observability/delivery_ratio/result.py`
   - `FamilyDeliveryRecord` (frozen `BaseModel`): `family`, `oracle: str | None`, `oracle_count: int |
     None`, `es_count: int`, `ratio: float | None`, `lost: int | None`, `status: FamilyStatus`,
     `min_ratio: float`, `zero_cause: str | None`.
   - `DeliveryRatioResultDoc` (frozen `BaseModel`): `run_at: datetime`, `since: date`, `until: date`,
     `status: ReportStatus`, `families: list[FamilyDeliveryRecord]`, `trace_id: str`, `kind: str =
     "system:delivery_ratio_probe"`.
   - `from_report(report: DeliveryReport, *, run_at: datetime, trace_id: str) ->
     DeliveryRatioResultDoc` — builds each `FamilyDeliveryRecord` by **explicit field-by-field
     construction** from each `FamilyDelivery` in `report.ranked_families` (worst-first, per AC-2's
     "carries the per-family verdict"), e.g. `FamilyDeliveryRecord(family=f.family, oracle=f.oracle,
     oracle_count=f.oracle_count, es_count=f.es_count, ratio=f.ratio, lost=f.lost, status=f.status,
     min_ratio=f.min_ratio, zero_cause=f.zero_cause.value if f.zero_cause else None)` — **not**
     `FamilyDeliveryRecord(**f.to_dict())` (codex finding 8: the `**kwargs` form silently drops a new
     `to_dict()` key under Pydantic's default extra-field handling instead of failing loudly; explicit
     construction makes a future schema drift a visible diff).

5. `src/personal_agent/observability/delivery_ratio/sink.py` — same shape as cache-erosion's, logs
   `delivery_ratio_result_indexed`.

6. `src/personal_agent/observability/delivery_ratio/scheduler_runner.py`
   - `run_scheduled_delivery_ratio_probe(*, es_client) -> DeliveryRatioResultDoc | None`. `None` early
     when disabled or `es_client is None`. Else: capture `run_at = datetime.now(timezone.utc)` once,
     derive `yesterday = run_at.date() - timedelta(days=1)` from it (codex finding 6 — not two
     separate `datetime.now()` calls). `_open_pg_conn()` (private helper, below); if it returns
     `None`, log and return `None`. Otherwise `collect_report(es_client, pg_conn,
     logs_prefix=settings.elasticsearch_index_prefix, since=yesterday, until=yesterday,
     min_ratio=settings.delivery_ratio_probe_min_ratio)` → `from_report(report, run_at=run_at,
     trace_id=ctx.trace_id)` → `write_result(...)` (write failure logged, not raised) → return doc,
     in a `finally: await _close_pg_conn(pg_conn)`.
   - `_open_pg_conn() -> Any | None` — `asyncpg.connect(_normalize_asyncpg_dsn(settings.database_url),
     timeout=10.0)` (a single connection, not a pool — design decision 2; `timeout` bounds how long a
     wedged Postgres can stall this scheduler's single serial lifecycle loop), imports
     `_normalize_asyncpg_dsn` from `personal_agent.llm_client.cost_tracker` (same helper the CLI
     already uses for this DSN), catches `Exception`, logs, returns `None`.
   - `_close_pg_conn(conn) -> None` — `await conn.close()` inside its own `try/except Exception: log.warning(...)`
     so a close failure cannot overwrite an already-successful return (codex finding 4 — mirrors
     `joinability/scheduler_runner.py._close()`'s per-resource try/except, not a bare `finally: await
     ...close()`).

7. `docker/elasticsearch/monitors-cache-erosion-index-template.json` — `index_patterns:
   ["agent-monitors-cache-erosion-*"]`, `priority: 100`, mapping: `run_at: date`, `window_days:
   integer`, `threshold: float`, `any_eroded: boolean`, `results: nested` (settled: `nested`, not plain
   `object` — design decision 9, codex finding 7) with `callsite: keyword`, `day_a/day_b: date`,
   `hash_count_a/hash_count_b: integer`, `jaccard: float`, `status: keyword`, **`threshold: float`**
   (codex finding 7 — the first draft's field list omitted this even though
   `CallsiteErosionRecord.threshold` is a real field; dynamic mapping would silently rescue it, but
   that defeats the point of an explicit template), `trace_id: keyword`, `kind: keyword`. Copy
   `dynamic_templates` block verbatim from `monitors-slm-health-index-template.json`
   (ms/ids/default-string-keyword rules).

8. `docker/elasticsearch/monitors-cache-erosion-ilm-policy.json` — copy
   `monitors-slm-health-ilm-policy.json` structure (hot/warm-32d-forcemerge/delete-90d); adjust
   `_meta.description`.

9. `docker/elasticsearch/monitors-delivery-ratio-index-template.json` — `index_patterns:
   ["agent-monitors-delivery-ratio-*"]`, mapping: `run_at: date`, `since/until: date`, `status:
   keyword`, `families: nested` (settled: `nested`, not plain `object` — same reasoning as #7) with
   `family/oracle: keyword`, `oracle_count/es_count/lost: integer`, `ratio: float`, `status: keyword`,
   `min_ratio: float`, `zero_cause: keyword`, `trace_id: keyword`, `kind: keyword`. Same
   dynamic_templates block.

10. `docker/elasticsearch/monitors-delivery-ratio-ilm-policy.json` — same shape as #8.

## Files to modify

1. **`src/personal_agent/config/settings.py`** — insert after the SLM-health block (after
   `slm_queue_depth_degraded`, ~line 1646), before the `# Consolidation Quality Monitor` comment:
   ```python
   # Cache-erosion monitor (ADR-0078 / FRE-1189)
   cache_erosion_probe_enabled: bool = Field(
       default=True,
       description="Enable scheduled cache-erosion probe runs (ADR-0078 / FRE-1189)",
   )
   cache_erosion_probe_interval_seconds: int = Field(
       default=3600,
       ge=60,
       description="Seconds between cache-erosion probe runs in the brainstem scheduler",
   )
   cache_erosion_probe_window_days: int = Field(
       default=2,
       ge=2,
       description="Consecutive-day comparison window for the cache-erosion probe",
   )
   cache_erosion_probe_threshold: float = Field(
       default=0.9,
       ge=0.0,
       le=1.0,
       description="Jaccard similarity floor; below this the probe reports erosion",
   )
   cache_erosion_probe_index_prefix: str = Field(
       default="agent-monitors-cache-erosion",
       description="Elasticsearch index prefix for cache-erosion probe result docs",
   )

   # Delivery-ratio probe (FRE-1051 / FRE-1189)
   delivery_ratio_probe_enabled: bool = Field(
       default=True,
       description="Enable scheduled delivery-ratio probe runs (FRE-1051 / FRE-1189)",
   )
   delivery_ratio_probe_interval_seconds: int = Field(
       default=86400,
       ge=300,
       description="Seconds between delivery-ratio probe runs in the brainstem scheduler",
   )
   delivery_ratio_probe_min_ratio: float = Field(
       default=0.99,
       ge=0.0,
       le=1.0,
       description=(
           "Delivery floor below which a family is a breach; mirrors "
           "observability.delivery_ratio.probe.DEFAULT_MIN_RATIO"
       ),
   )
   delivery_ratio_probe_index_prefix: str = Field(
       default="agent-monitors-delivery-ratio",
       description="Elasticsearch index prefix for delivery-ratio probe result docs",
   )
   ```

2. **`src/personal_agent/brainstem/scheduler.py`**
   - `__init__`, immediately after the SLM-health block (after line ~223), before the DomainGuard
     comment:
     ```python
     # Cache-erosion monitor (ADR-0078 / FRE-1189)
     self._last_cache_erosion_probe_run: datetime | None = None
     self.cache_erosion_probe_enabled = getattr(settings, "cache_erosion_probe_enabled", True)
     self.cache_erosion_probe_interval_seconds = getattr(
         settings, "cache_erosion_probe_interval_seconds", 3600
     )

     # Delivery-ratio probe (FRE-1051 / FRE-1189)
     self._last_delivery_ratio_probe_run: datetime | None = None
     self.delivery_ratio_probe_enabled = getattr(settings, "delivery_ratio_probe_enabled", True)
     self.delivery_ratio_probe_interval_seconds = getattr(
         settings, "delivery_ratio_probe_interval_seconds", 86400
     )
     ```
   - `_lifecycle_loop()`, immediately after the SLM-health gated block (~line 1102-1125), before the
     DomainGuard block: two gated blocks, structurally **similar but not identical** to the SLM-health
     one — the joinability/SLM-health blocks advance `_last_*_run = now` unconditionally after the
     `await`, because their runners never return `None` on a recoverable condition (only raise). This
     ticket's two runners *do* return `None` (disabled / no ES client / Postgres unreachable), so the
     new blocks must capture the return value and only advance on non-`None` (codex finding 2 — the
     first draft copied the unconditional-advance shape and that was a real bug, not a deliberate
     deviation):
     ```python
     # Hourly: cache-erosion probe (ADR-0078 / FRE-1189)
     if self.cache_erosion_probe_enabled and (
         self._last_cache_erosion_probe_run is None
         or (now - self._last_cache_erosion_probe_run).total_seconds()
         >= self.cache_erosion_probe_interval_seconds
     ):
         try:
             from personal_agent.observability.cache_erosion.scheduler_runner import (
                 run_scheduled_cache_erosion_probe,
             )

             cache_erosion_doc = await run_scheduled_cache_erosion_probe(
                 es_client=cast("AsyncElasticsearch | None", self._lifecycle_es_client)
             )
             if cache_erosion_doc is not None:
                 self._last_cache_erosion_probe_run = now
         except Exception as cache_erosion_err:
             log.warning(
                 "cache_erosion_probe_failed",
                 error=str(cache_erosion_err),
                 exc_info=True,
                 trace_id=iteration_trace_id,
             )

     # Daily: delivery-ratio probe (FRE-1051 / FRE-1189)
     if self.delivery_ratio_probe_enabled and (
         self._last_delivery_ratio_probe_run is None
         or (now - self._last_delivery_ratio_probe_run).total_seconds()
         >= self.delivery_ratio_probe_interval_seconds
     ):
         try:
             from personal_agent.observability.delivery_ratio.scheduler_runner import (
                 run_scheduled_delivery_ratio_probe,
             )

             delivery_ratio_doc = await run_scheduled_delivery_ratio_probe(
                 es_client=cast("AsyncElasticsearch | None", self._lifecycle_es_client)
             )
             if delivery_ratio_doc is not None:
                 self._last_delivery_ratio_probe_run = now
         except Exception as delivery_ratio_err:
             log.warning(
                 "delivery_ratio_probe_failed",
                 error=str(delivery_ratio_err),
                 exc_info=True,
                 trace_id=iteration_trace_id,
             )
     ```
     Note this means an ES *write* failure inside the runner (already swallowed one layer down, per
     design decision 7/file #3/#6) still returns a doc and still advances the timestamp — only a
     failure to *compute* the report at all (no ES client, Postgres unreachable, disabled) withholds
     it. That is the correct line: "the probe ran and produced a verdict" vs. "the probe could not
     run this tick", not "everything about this tick succeeded".

3. **`scripts/setup-elasticsearch.sh`** — after the existing joinability-substrate template block (the
   one at the end of the excerpt read during research, `put_and_apply_template
   "agent-monitors-joinability-substrate-template"`), add two more numbered blocks following the exact
   `put_resource` (ILM) → `put_and_apply_template` (mapping) ordering used for SLM-health/joinability:
   ```bash
   # N. Cache-erosion probe ILM policy + index template (ADR-0078 / FRE-1189).
   put_resource "ILM policy: agent-monitors-cache-erosion-policy" \
     "/_ilm/policy/agent-monitors-cache-erosion-policy" \
     "$PROJECT_ROOT/docker/elasticsearch/monitors-cache-erosion-ilm-policy.json"
   put_and_apply_template "Index template: agent-monitors-cache-erosion-template" \
     "/_index_template/agent-monitors-cache-erosion-template" \
     "$PROJECT_ROOT/docker/elasticsearch/monitors-cache-erosion-index-template.json"

   # N+1. Delivery-ratio probe ILM policy + index template (FRE-1051 / FRE-1189).
   put_resource "ILM policy: agent-monitors-delivery-ratio-policy" \
     "/_ilm/policy/agent-monitors-delivery-ratio-policy" \
     "$PROJECT_ROOT/docker/elasticsearch/monitors-delivery-ratio-ilm-policy.json"
   put_and_apply_template "Index template: agent-monitors-delivery-ratio-template" \
     "/_index_template/agent-monitors-delivery-ratio-template" \
     "$PROJECT_ROOT/docker/elasticsearch/monitors-delivery-ratio-index-template.json"
   ```
   (Exact insertion point / numbering confirmed by reading the live file at implementation time — the
   research read only an excerpt.)

## Tests

TDD order: write each test file failing first, then the module it targets.

1. `tests/observability/test_cache_erosion_sink.py` — mirrors `test_slm_health_sink.py`: `index_name_for`
   formats `<prefix>-YYYY-MM`; `write_result` calls `es.index` with a UUID id and a `document` dict
   whose `any_eroded` / `results[i].status` / `kind` match the input doc (AC-2's "carries the verdict").
2. `tests/observability/test_cache_erosion_scheduler_runner.py` — mirrors
   `test_slm_health_scheduler_runner.py`: returns `None` when disabled (mock `get_settings`); returns
   `None` and does not call `compute_erosion_report` when `es_client is None`; on success calls
   `write_result` once with a doc built from the (mocked) report; an ES write failure is swallowed
   (returns the doc, does not raise — the return value is what the scheduler's advance-timestamp check
   reads, so this case must return non-`None`).
3. `tests/observability/test_delivery_ratio_sink.py` — same shape as #1, asserting `families[i].status`
   including an `"unverifiable"` case round-trips into the written document (AC-2's explicit
   unverifiable-case requirement).
4. `tests/observability/test_delivery_ratio_scheduler_runner.py` — same shape as #2, plus: `_open_pg_conn`
   failure (mock `asyncpg.connect` to raise) → returns `None`, no ES write attempted; on success,
   `_close_pg_conn` is awaited even when `write_result` raises (verifies the `finally`); a
   `_close_pg_conn` failure itself is caught and logged, not propagated (codex finding 4 — verifies the
   fix, not just the happy path); assert the window passed to `collect_report` is `since == until ==
   yesterday`, both derived from one captured `run_at` (codex finding 6).
5. `tests/observability/test_cache_erosion.py` / `test_delivery_ratio*.py` — unchanged (probe/collect
   logic is out of scope; existing tests are the regression guard that it stayed untouched).
6. `tests/test_brainstem/test_scheduler.py` — two new test classes, each following the existing
   `TestDomainGuardWarm`-style "when due" / "when recent" / "survives failure" trio (single-tick,
   `asyncio.sleep` stubbed to stop after one iteration, `_last_*_run` set directly), **plus a fourth
   case this ticket's runners need that the existing trio doesn't cover** (codex finding 2 — the
   existing joinability/SLM-health probes never return `None` on a non-exceptional path, so no
   existing test exercises "ran, returned `None`, must not advance"):
   - `TestCacheErosionScheduling`: `test_lifecycle_loop_runs_probe_when_due`,
     `test_lifecycle_loop_skips_probe_when_recent`, `test_probe_failure_does_not_advance_timestamp`
     (raises), `test_probe_returning_none_does_not_advance_timestamp` (mock the runner to return `None`
     — e.g. disabled mid-flight or `es_client` absent — assert `_last_cache_erosion_probe_run` stays
     unset and the block does not raise).
   - `TestDeliveryRatioScheduling`: same four.
   - `TestCacheErosionSettingsWiring` / `TestDeliveryRatioSettingsWiring` (codex finding 3 — the
     compressed-interval test below proves the lifecycle gate honours an instance attribute, not that
     the attribute came from settings; this closes that gap directly, mirroring the existing
     `test_domain_guard_warm_interval_derived_from_ttl` pattern): construct `BrainstemScheduler()`
     while `personal_agent.brainstem.scheduler.settings` is patched with each new field set to a
     distinctive non-default value (e.g. `cache_erosion_probe_interval_seconds=1234`), assert the
     constructed instance's attribute equals it.

   Plus **one AC-1/AC-3 compressed-interval integration test per probe** (the "compressed-interval
   harness" the ticket's own check text names) — NOT single-tick:
   - Construct a real `BrainstemScheduler()`; set `scheduler.<probe>_interval_seconds = 0.05` and
     `_last_<probe>_run = None` directly as instance attributes (the settings-wiring tests above
     already prove these attributes come from settings at construction time; this test's job is only
     to prove the *lifecycle loop* honours whatever value is there).
   - **Capture `real_sleep = asyncio.sleep` before patching** (codex finding 1 — the first draft's stub
     called `asyncio.sleep(0.02)` from inside the replacement for
     `personal_agent.brainstem.scheduler.asyncio.sleep`, which resolves to the same patched name and
     recurses forever; this is a correctness bug in the test design, not a nitpick). Patch
     `personal_agent.brainstem.scheduler.asyncio.sleep` with an async stub that does `await
     real_sleep(0.02)` and flips `scheduler.running = False` after a fixed tick count (e.g. 6) — real
     wall-clock elapses between loop iterations, unlike the single-tick tests.
   - Patch the probe's substrate call one level below the runner (`compute_erosion_report` for
     cache-erosion; `collect_report` for delivery-ratio, plus `_open_pg_conn` returning a fake
     connection with `AsyncMock` `fetchval`/`close`) — NOT the runner function itself, so the real
     `run_scheduled_*`/`sink.write_result` code executes and a `time.monotonic()` timestamp is recorded
     each time the substrate call fires.
   - `scheduler._lifecycle_es_client = AsyncMock()`.
   - Assert the substrate call fired >= 2 times and the gap between the first two timestamps is >= half
     the configured interval — a lower-bound check only (codex: "do not assert a wall-clock upper
     bound; slow CI can violate it without a product defect"), still enough to show it did not fire
     every tick regardless of the interval.

## Quality gates (build skill Step 8)

`make test` (module: `make test-file FILE=tests/observability/test_cache_erosion_sink.py` etc., then
`make test-file FILE=tests/test_brainstem/test_scheduler.py`, then full `make test`) · `make mypy` ·
`make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.

Diff class: touches `src/` logic (scheduler.py, new observability modules, settings.py) → **Standard**,
not production-write-path/destructive/schema/cost-governance — self-serve review
(`feature-dev:code-reviewer` + `security-review`, since the diff touches network I/O to
Postgres/Elasticsearch) applies; no escalation trigger fires (reads only, no production write beyond
the probes' own result-doc index — same class as the existing joinability/SLM-health precedent, which
was never escalated for the equivalent write).

## Risk-tier classification (build skill Step 3)

**Standard** — touches `src/` logic (scheduler.py) and reads production Postgres + Elasticsearch.
**Codex plan-review required** before implementation.
