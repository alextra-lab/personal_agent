# FRE-1210 — T6: KG metrics + heatmap (Neo4j → Postgres projection, then a KG Health dashboard)

Backing design: `docs/superpowers/plans/2026-08-08-fre-1203-grafana-migration-program.md` § T6
(lines 532–655). This plan implements *this ticket's own* acceptance criteria (Linear FRE-1210,
rewritten 2026-08-11) — the design doc's sysgraph-proposal panels (items 11–12 in its § T6.2 list)
are **out of scope**: the Linear ticket text only names panels 1–10 and its AC-1..AC-5 never
reference `sysgraph.proposal`. Not folding those in; if wanted, they're a natural FRE-1211-adjacent
follow-on, not this ticket.

## Acceptance criteria (from the ticket — the definition of done)

| # | Criterion | How checked |
|---|---|---|
| AC-1 | The projection writes real rows | After one run, `SELECT count(*) FROM kg_stats WHERE metric_name='cold_mass_ratio'` ≥ 1, value matches a hand-run Cypher count of `access_count=0 ÷ total` |
| AC-2 | The heat is discriminating, not decorative | (a) no `access_count=0` entity in the top-10 by `access_count · e^(−λ·age)`; (b) that ranking reproduced independently in Cypher, and the two top-10 lists agree |
| AC-3 | The cold-mass number can move | Accessing a previously-cold entity decrements the never-read count on the next run |
| AC-4 | The heatmap renders with real buckets | Playwright screenshot of a populated `heatmap` panel, cross-checked against the same buckets computed directly in Cypher |
| AC-5 | The impossible panel is absent | No panel claims access-over-time; dashboard description states why |

## T6.1 — The projection

### New Cypher aggregation — `src/personal_agent/memory/kg_stats_aggregate.py` (new module)

Kept separate from `memory/freshness_aggregate.py` (staleness-tier scanning only) because this
computes a materially wider set of KG-health metrics, several with no staleness-tier relationship
(embedding reachability, duplicate names, turn coverage). Mirrors `freshness_aggregate.py`'s driver
session pattern (`async with driver.session() as session`, paged `SKIP/LIMIT` scans where a full
table scan is needed).

```python
@dataclass(frozen=True)
class KgStatRow:
    metric_name: str
    dimension: str | None
    metric_value: float

async def aggregate_kg_stats(driver: Any, settings: AppConfig | None = None) -> list[KgStatRow]:
    ...
```

One function, one Neo4j round-trip set, returns every row for one `kg_stats` write. Each metric:

1. **`entity_count` / `entity_type`** — `MATCH (e:Entity) RETURN e.entity_type AS t, count(e) AS n`
   (group by `coalesce(e.entity_type, 'UNKNOWN')`).
2. **`relationship_count` / `rel_type`** — `MATCH ()-[r]->() RETURN type(r) AS t, count(r) AS n`.
3. **`access_count_bucket` / `<bucket>`** — one paged scan
   `MATCH (e:Entity) RETURN coalesce(e.access_count,0) AS ac`, bucketed in Python into
   `0 / 1-2 / 3-5 / 6-10 / 11-25 / 26+` (ticket's own bucket boundaries — note these differ from the
   design doc's `access_count_bucket` boundaries table at line 604, which lists `0-1d`-style ranges
   under `recency_bucket` and a *different* 0/1-2/3-5/6-10/11-25/26+ set under `access_count_bucket`;
   use the ticket's table verbatim, not the doc's, if they ever diverge — ticket is authoritative
   per lifecycle-rules).
4. **`recency_bucket` / `<bucket>`** — same scan, `e.last_accessed_at`, bucketed by days-since into
   `0-1d / 2-7d / 8-14d / 15-30d / 31-60d / 60d+`; entities with `last_accessed_at IS NULL` are
   excluded from this histogram (they belong to `unmeasured_ratio`, not a recency bucket).
5. **`cold_mass_ratio`** — `MATCH (e:Entity) RETURN count(e) AS total, sum(CASE WHEN coalesce(e.access_count,0)=0 THEN 1 ELSE 0 END) AS never_read` → `never_read / total`. **Never-read is `access_count = 0`, not the DORMANT staleness tier** — DORMANT also includes old-but-previously-accessed entities; conflating them would silently redefine the headline number the ticket names. This is the one place this plan diverges in wording from `classify_staleness` and it's deliberate.
6. **`unmeasured_ratio`** — `MATCH (e:Entity) RETURN count(e) AS total, sum(CASE WHEN e.access_count IS NULL THEN 1 ELSE 0 END) AS unmeasured` → ratio. `IS NULL` here means "property absent" (Neo4j has no stored-null; a missing property and an explicit null both read back `NULL`), so this is distinct from `access_count = 0` above — verified against FRE-161's four-property write path (properties are absent until the freshness consumer first touches the entity).
7. **`embedding_missing`** — reuse the exact predicate from `memory/service.py:_backfill_entity_embeddings` (FRE-659): `MATCH (e:Entity) WHERE e.embedding IS NULL OR none(x IN e.embedding WHERE x <> 0.0) RETURN count(e)`. Report as a raw count (metric doc calls it a count, not a ratio — matches the ticket's metrics table, `dimension` = `—`).
8. **`duplicate_group_count` / `type_disagreement_count`** — one query:
   ```cypher
   MATCH (e:Entity)
   WITH toLower(e.name) AS lname, collect(DISTINCT e.entity_type) AS types, count(e) AS n
   WHERE n > 1
   RETURN count(*) AS duplicate_groups,
          sum(CASE WHEN size(types) > 1 THEN 1 ELSE 0 END) AS type_disagreements
   ```
9. **`turns_without_entities_ratio`** — `Turn` nodes connect to `Entity` via `[:DISCUSSES]` (confirmed live pattern, `memory/service.py:376` and 15+ other call sites):
   ```cypher
   MATCH (t:Turn)
   WITH t, EXISTS((t)-[:DISCUSSES]->(:Entity)) AS has_entity
   RETURN count(t) AS total, sum(CASE WHEN has_entity THEN 0 ELSE 1 END) AS without_entities
   ```

All ratios are `dimension = NULL`; bucketed/typed metrics carry their bucket/type string as `dimension`.

### `kg_stats` table — migration `docker/postgres/migrations/0026_kg_stats.sql`

Exact DDL from the design doc (already owner-approved, quoted verbatim in both the plan and the
Linear ticket):

```sql
-- FRE-1210 (T6.1): daily Neo4j→Postgres projection of KG health/freshness metrics.
-- Written by brainstem.jobs.freshness_review in addition to the existing JSONL
-- snapshot (ADR-0054 D4: durable-first, bus-second — JSONL write is not removed).
CREATE TABLE IF NOT EXISTS kg_stats (
    id              BIGSERIAL PRIMARY KEY,
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric_name     VARCHAR(64) NOT NULL,
    dimension       VARCHAR(64),
    metric_value    DOUBLE PRECISION NOT NULL,
    UNIQUE (observed_at, metric_name, dimension)
);
CREATE INDEX idx_kg_stats_metric_time ON kg_stats(metric_name, observed_at DESC);

GRANT SELECT ON public.kg_stats TO grafana_ro;
```

`seshat_app` needs no explicit grant — `0015_app_role_grants.sql`'s `ALTER DEFAULT PRIVILEGES`
covers any new `public` table created by the `agent` superuser. `grafana_ro` does need the explicit
grant — `0025`'s migration deliberately has no `ALTER DEFAULT PRIVILEGES` on `public` for that role
(future tables are granted individually, on purpose).

Run via `AGENT_DATABASE_ADMIN_URL` per the pre-merge checklist (`agent` superuser, not `seshat_app`).

Mirror the same `CREATE TABLE`/`CREATE INDEX`/`GRANT` block into `docker/postgres/init.sql`,
alongside the other table definitions (near `budget_policies` et al.), and append `public.kg_stats`
to the existing `grafana_ro` GRANT list at the bottom of that file — `test_init_sql_model_parity.py`
builds its schema from `init.sql` alone, so a migration-only change is invisible to it.

### SQLAlchemy model — `src/personal_agent/service/models.py`

```python
class KgStatModel(Base):
    __tablename__ = "kg_stats"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    metric_name = Column(String(64), nullable=False)
    dimension = Column(String(64), nullable=True)
    metric_value = Column(Float, nullable=False)
```

This is what makes `test_init_sql_model_parity.py::test_every_model_column_exists_in_init_sql` pick
up `kg_stats` at all (it iterates `Base.metadata.tables`, not raw `init.sql`).

### Postgres writer — `src/personal_agent/memory/kg_stats_aggregate.py` (same module)

Copy the `observability/delivery_ratio/scheduler_runner.py` pattern exactly: **one short-lived
`asyncpg.connect()`**, not a pool — daily cadence, one sequential write, no concurrency to justify
pool overhead (that file's own docstring makes this argument; it applies identically here).

```python
async def write_kg_stats(rows: Sequence[KgStatRow], trace_id: str) -> int:
    """Write one projection run's rows to kg_stats. Returns rows written, 0 on failure."""
    conn = await _open_pg_conn()  # settings.database_url, _normalize_asyncpg_dsn — copy from delivery_ratio
    if conn is None:
        log.warning("kg_stats_write_skipped_no_pg", trace_id=trace_id)
        return 0
    observed_at = datetime.now(timezone.utc)
    try:
        await conn.executemany(
            """
            INSERT INTO kg_stats (observed_at, metric_name, dimension, metric_value)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (observed_at, metric_name, dimension) DO NOTHING
            """,
            [(observed_at, r.metric_name, r.dimension, r.metric_value) for r in rows],
        )
        return len(rows)
    finally:
        await conn.close()
```

All rows in one run share one `observed_at` timestamp (captured once) so they form one queryable
snapshot row-set per run — required for AC-1's "after one scheduled run" framing and for panels
that trend `metric_value` over `observed_at`.

### Wiring into `freshness_review.py`

In `run_freshness_review()`, after the existing `_write_snapshot(...)` call (line 306) and before
the Captain's Log proposal loop — durable-first still holds since the existing tier-snapshot JSONL
write already landed by that point:

```python
kg_rows = await aggregate_kg_stats(memory_service.driver, cfg)
written = await write_kg_stats(kg_rows, trace_id)
log.info("kg_stats_projection_completed", trace_id=trace_id, rows_written=written)
```

Gated the same way the rest of the job already is — no new `cfg.*_enabled` flag needed; this runs
whenever `freshness_enabled` is true, since it's part of the same review pass, not a separable
feature. A projection failure (returns 0) is logged, not raised — matches the job's existing
swallow-and-log posture for CL proposals and bus publish.

### Cadence: weekly → daily

Ticket requires this explicitly ("weekly is too coarse for a trend and the scan is cheap").

- `config/settings.py`: change `freshness_review_schedule_cron` default from `"0 3 * * 0"` to
  `"0 3 * * *"`.
- `brainstem/jobs/freshness_review.py::parse_freshness_review_schedule`: currently maps a `*`
  weekday field to `_DEFAULT_PYTHON_WEEKDAY` (Sunday) — i.e. `*` is silently treated as "Sunday
  only", which is wrong for "every day". Change the return type to `tuple[int, int, int | None]`
  where `None` means "any day", and return `None` when `dow_s == "*"`.
- `brainstem/scheduler.py` (~lines 1255–1287): the dedup guard currently tracks
  `self._last_freshness_review_week: tuple[int, int] | None` keyed on `(year, week)`. For daily
  cadence this must become a per-day key. Rename to `self._last_freshness_review_date: date | None`,
  compare `now.date() != self._last_freshness_review_date`, and the trigger condition becomes
  `(fr_weekday is None or now.weekday() == fr_weekday) and now.hour == fr_hour and now.minute == fr_minute`.
  This is a rename + behavior change on one field — grep `_last_freshness_review_week` across
  `src/` and `tests/` before editing to catch every reference (expect the scheduler class attribute,
  its `__init__` default, and any test that pokes it directly).
- `trace_id` format `freshness-review-{year}-W{week:02d}` (scheduler.py) stays week-grained for
  human readability even though the job now runs daily — no ticket requirement to change it, leave
  as-is (the JSONL filename `FR-<iso_week>.jsonl` also stays week-grained; multiple daily runs in
  the same week append to the same file, which is what "append" already does — no code change
  needed there, just confirm the append behavior is still correct at higher frequency).

### Tests (TDD — write failing first)

- `tests/test_memory/test_kg_stats_aggregate.py` (new): one test per metric above, against a fake/mock
  Neo4j async driver — check `tests/test_memory/` for the existing driver-mocking fixture used by
  other `memory/service.py` or `memory/dedup.py` tests and copy that pattern (do not hand-roll a new
  mock shape). Cover: empty graph (all metrics zero/empty, no divide-by-zero on `cold_mass_ratio`/
  `unmeasured_ratio` when `total=0`), a graph with a mix of measured/unmeasured/never-read entities
  (asserts `cold_mass_ratio` and `unmeasured_ratio` are computed from *different* predicates — this
  is the test that would catch a conflated implementation), duplicate-name entities with matching
  and mismatched `entity_type` (asserts `duplicate_group_count` vs `type_disagreement_count`
  distinction), and a `Turn` with and without `[:DISCUSSES]` edges.
- `tests/test_memory/test_kg_stats_write.py` (new, `@pytest.mark.integration`, redirected to `:5433`
  per FRE-375 — get the DSN from `settings`, never hardcode): round-trips `write_kg_stats` against
  the test Postgres stack, asserts `ON CONFLICT DO NOTHING` doesn't raise on a re-run with the same
  `observed_at`.
- `tests/test_brainstem/test_freshness_review_schedule.py` (existing — extend): add a case for
  `parse_freshness_review_schedule("0 3 * * *")` returning weekday `None`, and a case for the
  scheduler's per-day dedup guard firing once per calendar day rather than once per ISO week.
- `tests/migrations/test_init_sql_model_parity.py`: no new test needed — `KgStatModel` existing on
  `Base` is what makes the existing parametrized test pick up `kg_stats` automatically. Run it to
  confirm.
- **AC-3 probe** (cold-mass can move): an integration test against the test Neo4j+Postgres stack —
  create an entity with `access_count=0`, run `aggregate_kg_stats` + `write_kg_stats`, assert
  `cold_mass_ratio` reflects it as never-read; bump `access_count` to simulate an access, re-run,
  assert the new `cold_mass_ratio` row is strictly lower. This is the literal AC-3 wording as a test,
  not an inference from AC-1/AC-2 passing.

## T6.2 — The KG Health dashboard

**Governed by the `create-visualization` skill — invoke it, do not hand-author JSON.** Ten panels on
`pg-ledger`, tagged `grafana-native`, file `config/grafana/dashboards/kg_health.json`.

| # | Panel | Type | Query shape (against `kg_stats`) |
|---|---|---|---|
| 1 | Cold-mass trend | `timeseries`, unit `percentunit`, threshold step at 0.3 | `SELECT observed_at, metric_value FROM kg_stats WHERE metric_name='cold_mass_ratio' ORDER BY observed_at` |
| 2 | Heat histogram | `bargauge` | latest `observed_at` per `dimension` WHERE `metric_name='access_count_bucket'` |
| 3 | Recency × frequency heatmap | `heatmap` | latest run, cross join `access_count_bucket` × `recency_bucket` dimensions — **needs its own combined metric or a join query**; see note below |
| 4 | Type × recency heatmap | `heatmap` | `entity_type` × `recency_bucket` — same join-shape issue |
| 5 | Node counts by type over time | `timeseries` | `metric_name='entity_count'`, series per `dimension` |
| 6 | Edge counts by type over time | `timeseries` | `metric_name='relationship_count'`, series per `dimension` |
| 7 | Embedding reachability trend | `timeseries` or `stat` | `metric_name='embedding_missing'` over time |
| 8 | Duplicate-group / type-disagreement counts | `stat` (×2) or `table` | `metric_name IN ('duplicate_group_count','type_disagreement_count')`, latest |
| 9 | Turns-without-entities rate | `timeseries`, unit `percentunit` | `metric_name='turns_without_entities_ratio'` |
| 10 | Growth per active day | `barchart` | day-over-day delta of `entity_count` total (sum across dimensions) |

**Panels 3–4 (the heatmaps) are the one design gap this plan does not fully close before Step 0's
Cypher work**: the `kg_stats` schema as specified (`metric_name`, one `dimension`) cannot natively
express a two-dimensional cross-tab (recency × frequency, or type × recency) — each row is one
metric/dimension pair, not a matrix cell. Two ways to close this, decide during T6.1 implementation
(not a deferred design decision — resolve before writing the Cypher, since it changes what rows
`aggregate_kg_stats` emits for these two panels):
  - **(a)** Emit a *combined* `dimension` string per matrix cell, e.g.
    `metric_name='recency_frequency_cell'`, `dimension='0-1d|0'` (pipe-joined bucket pair), and let
    the Grafana heatmap panel's "already bucketed" mode consume it directly (`Format as: Heatmap
    cells`, per the Grafana Postgres datasource docs — read them first per the skill's
    documentation-first rule, do not assume the format from memory).
  - **(b)** Grafana's native heatmap panel can calculate buckets itself from raw (non-pre-bucketed)
    numeric input — if so, feed it entity-level `access_count`/`last_accessed_at`/`entity_type` rows
    directly from a *different* source than `kg_stats` (a raw Postgres view, or accept that this
    needs its own un-aggregated table/query) rather than the pre-aggregated `kg_stats` rows.
  Read the Grafana v13.1 heatmap panel docs (skill § Documentation-first names the exact URLs) before
  picking (a) vs (b) — do not guess from what "seems reasonable." This is flagged explicitly rather
  than silently resolved because it's the one place the ticket's own `kg_stats` DDL and its own
  panel list are in tension, and getting it wrong produces exactly the "renders but means nothing"
  failure the skill exists to prevent.

**Dashboard description** (required by AC-5, and by the design doc's "state honestly" instruction)
must say, in the dashboard's own `description` field or a text panel:
- Access-over-time (day×hour) is not shown because Neo4j retains only the last access — the history
  is discarded at write time, not merely unqueried.
- Panels 1–4 will be near-empty at merge (no history until the daily projection has run for several
  days) — this is expected, not a bug.

**AC-2 discrimination check** — build as a documented probe, not just eyeballed on the dashboard:
a script/test that (a) computes top-10 by `access_count · e^(−λ·age)` directly in Cypher against
live Neo4j, asserts no `access_count=0` entity appears in it, and (b) recomputes the same ranking
from whatever `kg_stats` exposes (or, if `kg_stats`'s aggregate rows don't carry enough resolution
for entity-level ranking — likely, since `kg_stats` is aggregate-only — this AC may need to be
checked directly in Cypher twice, independently, rather than through the Postgres projection at all;
re-read the ticket wording carefully here: "the panel's ranking is reproduced independently" doesn't
require the *panel itself* to be backed by entity-level Postgres data, only that its displayed
ranking — however sourced — agrees with an independent Cypher computation). Decide this before
building panel content, since it affects whether any panel here needs entity-level (not just
aggregate) Postgres data.

### Follow the `create-visualization` skill's Arm A exactly for the build

Ephemeral Editor instance → scratch dashboard → per-panel: pick viz type first, set query, set
`fieldConfig.defaults.unit` + thresholds (bargauge/stat panels need thresholds per Gate 0) → extract
via `GET /api/dashboards/uid/<uid>` (never the Settings → JSON Model tab, wrong schema on 13.1.3) →
commit → tear down → verify against a throwaway worktree-mounted Viewer instance (this worktree is
`build2`, not the primary `/opt/seshat` checkout, so the shared `cloud-sim-grafana` instance mounts
the wrong tree — stand up the second throwaway instance per the skill's explicit worktree warning).

Run Gate 0 (`jq` field-config gate) against the new file, the Grafana render-assert, and the AC-4
Playwright screenshot + Cypher bucket cross-check before calling T6.2 done.

## Risk tier and review

**Standard/Complex** — new Postgres schema (migration + model), a scheduler cadence/dedup behavior
change, new Cypher aggregation logic, and a Grafana dashboard build. **codex plan-review required**
before implementation, then explicit owner approval, per the build skill.

## Test commands

```bash
make test-file FILE=tests/test_memory/test_kg_stats_aggregate.py
make test-file FILE=tests/test_memory/test_kg_stats_write.py
make test-file FILE=tests/test_brainstem/test_freshness_review_schedule.py
make test-file FILE=tests/migrations/test_init_sql_model_parity.py
make test                  # full suite
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```
