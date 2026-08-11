# FRE-1210 — T6: KG metrics + heatmap (Neo4j → Postgres projection, then a KG Health dashboard)

Backing design: `docs/superpowers/plans/2026-08-08-fre-1203-grafana-migration-program.md` § T6
(lines 532–655). This plan implements *this ticket's own* acceptance criteria (Linear FRE-1210,
rewritten 2026-08-11) — the design doc's sysgraph-proposal panels (items 11–12 in its § T6.2 list)
are **out of scope**: the Linear ticket text only names panels 1–10 and its AC-1..AC-5 never
reference `sysgraph.proposal`.

**Revision 2 (2026-08-11)** — incorporates a codex adversarial plan-review (`task-msoo2kgh-hf3a14`,
run before any code was written). Every finding below is a fix folded into this revision, not a
follow-up ticket; nothing here is scope creep, it's correcting the plan before it becomes wrong code.

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

Kept separate from `memory/freshness_aggregate.py` (staleness-tier scanning only). Mirrors that
module's driver-session pattern.

```python
@dataclass(frozen=True)
class KgStatRow:
    metric_name: str
    dimension: str | None
    metric_value: float

async def aggregate_kg_stats(driver: Any, settings: AppConfig | None = None) -> list[KgStatRow]:
    ...
```

**Owner exclusion applies to every entity-level query below** (codex finding 1.3): the owner node is
`:Person:Entity` with `user_id` set (FRE-632, ADR-0052 amendment) and is deliberately excluded from
every existing graph-health metric (`quality_monitor.py:196-198, 259-260`) — counting it skews
extraction-quality rates. Every `MATCH (e:Entity)` query here adds `WHERE e.user_id IS NULL`.

1. **`entity_count` / `entity_type`** — `MATCH (e:Entity) WHERE e.user_id IS NULL RETURN coalesce(e.entity_type,'UNKNOWN') AS t, count(e) AS n`.
2. **`relationship_count` / `rel_type`** — **scoped to semantic KG edges, not every Neo4j edge**
   (codex finding 1.7): `MATCH ()-[r]->() WHERE type(r) IN ["RELATIONSHIP", "DISCUSSES"] RETURN type(r) AS t, count(r) AS n`
   — the exact type list `quality_monitor.py`'s `check_graph_health` already uses (line ~272).
   Infrastructure/provenance edges (`CONTAINS`, `NEXT`, `PARTICIPATED_IN`, `OPERATED_BY`, description-history
   edges) are excluded; verify the constant against `quality_monitor.py` at implementation time and
   import it rather than re-typing the literal list if one is exported.
3. **`access_count_bucket` / `<bucket>`** — paged scan `MATCH (e:Entity) WHERE e.user_id IS NULL RETURN coalesce(e.access_count,0) AS ac`,
   bucketed in Python into `0 / 1-2 / 3-5 / 6-10 / 11-25 / 26+` (ticket's own table — authoritative
   over the design doc if they ever diverge).
4. **`recency_bucket` / `<bucket>`** — **restricted to entities that were actually accessed**
   (codex finding 1.2): `create_entity` sets `last_accessed_at = datetime()` **at creation time**,
   with `access_count=0` (`memory/service.py:2143`) — so a plain `last_accessed_at IS NOT NULL` scan
   would misclassify never-read entities as "recently accessed." Query:
   `MATCH (e:Entity) WHERE e.user_id IS NULL AND coalesce(e.access_count,0) > 0 AND e.last_accessed_at IS NOT NULL RETURN e.last_accessed_at AS ts`,
   bucketed by days-since into `0-1d / 2-7d / 8-14d / 15-30d / 31-60d / 60d+`. Entities with
   `access_count=0` never appear in this histogram at all — they belong to `cold_mass_ratio`.
5. **`cold_mass_ratio`** — **fixed predicate** (codex finding 1.1, the most consequential Cypher bug
   in the original draft): the original numerator used `coalesce(e.access_count,0)=0`, which
   silently folded "never measured" entities into "never read," duplicating `unmeasured_ratio`'s
   population instead of partitioning against it. Correct query:
   ```cypher
   MATCH (e:Entity) WHERE e.user_id IS NULL
   RETURN count(e) AS total, sum(CASE WHEN e.access_count = 0 THEN 1 ELSE 0 END) AS never_read
   ```
   `e.access_count = 0` matches only entities with the property present and literally zero — a
   missing property compares to `NULL` (falsy), so it's excluded automatically, no `coalesce` needed
   or wanted here. `cold_mass_ratio = never_read / total`. `total` is **all non-owner entities**
   (matches the design doc's cohort table, where "Never read" 36.3% and "Unmeasurable" 13.0% are
   both slices of the same 100%, not of two different denominators).
6. **`unmeasured_ratio`** — `MATCH (e:Entity) WHERE e.user_id IS NULL RETURN count(e) AS total, sum(CASE WHEN e.access_count IS NULL THEN 1 ELSE 0 END) AS unmeasured`
   → ratio. `IS NULL` = property absent (Neo4j has no stored-null; a missing property and an explicit
   null both read back `NULL`). Now genuinely disjoint from `cold_mass_ratio`'s population.
7. **`embedding_missing`** — **definition decided explicitly** (codex finding 1.5): this metric means
   *"entities unreachable by vector recall,"* the full `Entity` population (minus owner), **not**
   `_backfill_entity_embeddings`'s narrower backfill-eligible population (which additionally requires
   a non-empty `description`, `memory/service.py:2350`) — a description-less, embedding-less entity
   is still unreachable by vector search and arguably the more important case to surface, not a case
   to silently exclude. Query:
   `MATCH (e:Entity) WHERE e.user_id IS NULL AND (e.embedding IS NULL OR none(x IN e.embedding WHERE x <> 0.0)) RETURN count(e)`.
   Report as a raw count (`dimension = NULL`), matching the ticket's metrics table.
8. **`duplicate_group_count` / `type_disagreement_count`** — **normalized to match the existing
   convention** (codex finding 1.4): the original draft grouped on bare `toLower(e.name)` with no
   trim/blank/owner filter. `quality_monitor.py`'s duplicate query (line ~202) uses
   `toLower(trim(e.name))` and rejects `normalized_name = ""`. Adopt the same normalization:
   ```cypher
   MATCH (e:Entity) WHERE e.user_id IS NULL
   WITH toLower(trim(e.name)) AS lname, collect(DISTINCT e.entity_type) AS types, count(e) AS n
   WHERE lname <> "" AND n > 1
   RETURN count(*) AS duplicate_groups,
          sum(CASE WHEN size(types) > 1 THEN 1 ELSE 0 END) AS type_disagreements
   ```
9. **`turns_without_entities_ratio`** — `DISCUSSES` confirmed as the sole production Turn→Entity edge
   (codex finding 1.6 — no fix needed; `service.py:1292`, and `store_episode` routes through the same
   writer rather than a separate `:Episode` path, `protocol_adapter.py:144-170`):
   ```cypher
   MATCH (t:Turn)
   WITH t, EXISTS((t)-[:DISCUSSES]->(:Entity)) AS has_entity
   RETURN count(t) AS total, sum(CASE WHEN has_entity THEN 0 ELSE 1 END) AS without_entities
   ```
10. **`top_heat_entity` / `<entity name>`** — **new metric, added to satisfy AC-2** (codex finding
    4.1: the original ten-panel list had no panel capable of showing the required entity-level
    top-10 ranking, so AC-2 was unimplementable as drafted). λ is defined as
    `ln(2) / cfg.freshness_half_life_days` (reuses the existing half-life setting — the same decay
    constant that already governs the WARM tier boundary, so this isn't a new free parameter):
    ```cypher
    MATCH (e:Entity)
    WHERE e.user_id IS NULL AND coalesce(e.access_count,0) > 0 AND e.last_accessed_at IS NOT NULL
    WITH e.name AS name, e.access_count AS ac,
         duration.between(e.last_accessed_at, datetime()).days AS age_days
    RETURN name, ac * exp(-$lambda * age_days) AS score
    ORDER BY score DESC
    LIMIT 10
    ```
    `access_count=0` entities are excluded by the `WHERE` clause itself (not merely scored zero),
    which makes AC-2(a) — "no `access_count=0` entity ever appears in the top-10" — true by
    construction, not by luck of the scoring formula. This becomes **an 11th panel** (see T6.2) —
    the panel count grows from 10 to 11, directly because AC-2 requires it; not scope creep.

All ratio/count metrics carry `dimension = NULL`; bucketed/typed/ranked metrics carry their
bucket/type/entity-name string as `dimension`.

### `kg_stats` table — migration `docker/postgres/migrations/0026_kg_stats.sql`

**Two deliberate deviations from the DDL as literally quoted in the ticket/design doc**, both
required by findings below — call these out explicitly in the PR description, since a schema
deviation from an owner-quoted DDL deserves visibility even though the quoted DDL was itself a
worked suggestion, not a hard external contract:

1. **`dimension` widened from `VARCHAR(64)` to `VARCHAR(255)`.** Two things exceed 64 chars: entity
   names stored as `dimension` for the new `top_heat_entity` metric (item 10 above), and the
   pipe-joined bucket-pair strings the heatmap panels need (see T6.2). 64 was sized for short bucket
   labels only.
2. **`UNIQUE NULLS NOT DISTINCT` on the constraint** (codex finding 4.4): Postgres's default
   `UNIQUE` treats every `NULL` as distinct from every other `NULL`, so two rows with the same
   `(observed_at, metric_name)` and `dimension = NULL` (every ratio/count metric) would **not**
   conflict and the "prevent duplicate rows from a re-run at the identical instant" guarantee the
   `ON CONFLICT DO NOTHING` writer relies on silently does nothing for exactly the metrics that use
   `NULL` dimensions. `NULLS NOT DISTINCT` was added in Postgres 15; the running stack is Postgres 17
   (`datasources.yaml` `postgresVersion: 1700`), so it's available.

```sql
-- FRE-1210 (T6.1): daily Neo4j→Postgres projection of KG health/freshness metrics.
-- Written by a dedicated daily job (kg_stats_projection.py) alongside — not replacing —
-- the existing weekly freshness_review JSONL snapshot (ADR-0054 D4: durable-first).
CREATE TABLE IF NOT EXISTS kg_stats (
    id              BIGSERIAL PRIMARY KEY,
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric_name     VARCHAR(64) NOT NULL,
    dimension       VARCHAR(255),
    metric_value    DOUBLE PRECISION NOT NULL,
    UNIQUE NULLS NOT DISTINCT (observed_at, metric_name, dimension)
);
CREATE INDEX idx_kg_stats_metric_time ON kg_stats(metric_name, observed_at DESC);

GRANT SELECT ON public.kg_stats TO grafana_ro;
```

`seshat_app` needs no explicit grant (`0015_app_role_grants.sql`'s `ALTER DEFAULT PRIVILEGES`
covers it). `grafana_ro` needs the explicit grant (`0025`'s migration deliberately has no
`ALTER DEFAULT PRIVILEGES` on `public` for that role). Run via `AGENT_DATABASE_ADMIN_URL`.

Mirror the identical `CREATE TABLE`/`CREATE INDEX`/`GRANT` into `docker/postgres/init.sql` (near the
other table definitions), and append `public.kg_stats` to the `grafana_ro` GRANT list at the bottom
of that file.

### SQLAlchemy model — `src/personal_agent/service/models.py`

```python
class KgStatModel(Base):
    __tablename__ = "kg_stats"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    observed_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    metric_name = Column(String(64), nullable=False)
    dimension = Column(String(255), nullable=True)
    metric_value = Column(Float, nullable=False)
```

`server_default=text("NOW()")` mirrors the DDL default (codex finding 4.7 — the parity test only
compares column *names*, so a missing default is invisible to it; get it right here directly rather
than relying on that test to catch it).

### Postgres writer — `src/personal_agent/memory/kg_stats_aggregate.py` (same module)

One short-lived `asyncpg.connect()`, copying `observability/delivery_ratio/scheduler_runner.py`'s
pattern exactly (daily cadence, one sequential write — a pool buys nothing here, and that file's own
docstring makes the identical argument).

```python
async def write_kg_stats(rows: Sequence[KgStatRow], trace_id: str) -> int:
    """Write one projection run's rows to kg_stats. Returns rows written, 0 on any failure."""
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
    except Exception as exc:
        log.warning("kg_stats_write_failed", trace_id=trace_id, error=str(exc), exc_info=True)
        return 0
    finally:
        await conn.close()
```

**Fixed from the original draft** (codex finding 4.5): the write previously had only a `finally`
block, so an `executemany` error would propagate rather than honoring the function's own documented
"returns 0 on failure" contract. Now caught and logged explicitly at this boundary — belt-and-braces
alongside the scheduler's own outer `try/except` around the whole job call (already present,
`scheduler.py:~1278`), but the writer's own contract should hold regardless of the caller.

All rows in one run share one `observed_at` timestamp (captured once), forming one queryable
snapshot per run.

### A separate daily job — NOT a cadence change to the existing weekly `freshness_review`

**This is the largest change from the first draft, and directly resolves codex's biggest finding**
(2.1–2.3): the original plan proposed changing `freshness_review`'s own schedule from weekly to
daily. But `run_freshness_review()` isn't just a Cypher scan — it also drives `MemoryStalenessReviewedEvent`
(keyed by `iso_week`, `events/models.py:137`), a fingerprint computed from `(dominant_tier, iso_week)`
that becomes the downstream Captain's Log proposal's fingerprint (`pipeline_handlers.py:1052`), a
consumer whose title/rationale literally say "Weekly freshness review" (`pipeline_handlers.py:1025`),
and settings/docstring prose that call it a weekly job throughout. Making the whole job daily without
touching any of that would fire seven semantically-"weekly" events a week, each carrying an
`iso_week` that doesn't identify which of the week's seven runs produced it, and colliding
fingerprints across those runs (finding 2.2) — a real correctness bug in the downstream pipeline, not
a cosmetic one.

The ticket's actual requirement — "weekly is too coarse for a trend and the scan is cheap," applied
to the *projection* — doesn't require the *whole* staleness-tier/JSONL/CL-proposal/bus-event pipeline
to run more often. **Decouple them**: leave `run_freshness_review()` and its weekly cadence, event
contract, fingerprint, and consumer copy completely untouched (zero ripple into that pipeline), and
add a new, independently-scheduled daily job purely for the Postgres projection.

- **New file** `src/personal_agent/brainstem/jobs/kg_stats_projection.py`:
  ```python
  async def run_kg_stats_projection(memory_service: MemoryService | None, trace_id: str) -> None:
      cfg = get_settings()
      if not cfg.freshness_enabled:  # same gate freshness_review uses — same underlying FRE-161 data
          log.debug("kg_stats_projection_skipped_disabled", trace_id=trace_id)
          return
      if memory_service is None or not memory_service.connected or memory_service.driver is None:
          log.warning("kg_stats_projection_skipped_no_memory", trace_id=trace_id)
          return
      rows = await aggregate_kg_stats(memory_service.driver, cfg)
      written = await write_kg_stats(rows, trace_id)
      log.info("kg_stats_projection_completed", trace_id=trace_id, rows_written=written)
  ```
  No new `*_enabled` setting — gated on the existing `freshness_enabled` flag, since it reads the
  same FRE-161 access-tracking properties; a second flag would be config sprawl the ticket doesn't
  ask for.
- **New setting** `config/settings.py`: `kg_stats_projection_schedule_cron: str = Field(default="0 4 * * *", ...)`
  — daily, offset one hour from the existing weekly freshness job's 3 AM to avoid overlapping Neo4j
  scan load on the one day a week they'd otherwise coincide.
- **New scheduler wiring** `brainstem/scheduler.py`, alongside the existing per-day dedup fields
  (`self._last_quality_check_date`, `self._last_feedback_date` — the established pattern for daily
  jobs in this file, `scheduler.py:163-164`): add `self._last_kg_stats_projection_date: date | None = None`
  and a trigger block structured like the existing daily ones, comparing `now.date()` rather than
  `(year, week)`. Uses `parse_freshness_review_schedule` unchanged (it already returns
  `(minute, hour, weekday)` and already handles a `*` dow field — verify its current `*` handling
  maps to "every day" and not silently to Sunday-only before reusing it verbatim; if it currently
  forces `*` to Sunday, either fix that one behavior or, more surgically, write a tiny dedicated
  daily-schedule parser for this one new cron string instead of stretching the weekly parser's
  semantics — decide during implementation, whichever is the smaller diff once the current code is
  in front of you).

This design change means: no changes to `events/models.py`, `pipeline_handlers.py`, the CL proposal
fingerprinting, or `settings.freshness_review_schedule_cron`'s existing weekly meaning. Codex findings
2.1, 2.2, and 2.3 are resolved by not touching that surface at all, rather than by chasing every place
"weekly" is written in prose.

### Tests (TDD — write failing first)

- `tests/test_memory/test_kg_stats_aggregate.py` (new, mock driver): one case per metric, plus:
  owner exclusion (an owner `:Person:Entity` node present in the fixture must not appear in any
  count), the `cold_mass_ratio` vs `unmeasured_ratio` disjointness case (a graph with both an
  `access_count IS NULL` entity and an `access_count = 0` entity — assert each is counted in exactly
  one of the two metrics, not both, not neither — this is the test that would have caught the
  original conflation bug), the recency-bucket exclusion of `access_count=0` entities despite having
  a `last_accessed_at` set at creation, duplicate-name normalization (mixed case, leading/trailing
  whitespace, and a blank-name entity that must not form a group), and `top_heat_entity` excluding
  every `access_count=0` entity from its result set entirely (not just scoring them low).
- `tests/test_memory/test_kg_stats_write.py` (new, `@pytest.mark.integration`, redirected to `:5433`
  per FRE-375): round-trips `write_kg_stats`; asserts a same-timestamp re-run of a `dimension=NULL`
  row is a true no-op under `ON CONFLICT DO NOTHING` (the case `UNIQUE NULLS NOT DISTINCT` fixes —
  this test is what proves the fix, not just the DDL comment); asserts a write failure (e.g. a closed
  connection injected) returns `0` rather than raising.
- `tests/migrations/test_0026_kg_stats_migration.py` (new — codex finding 4.6, the existing parity
  test only diffs column *names* against `init.sql`, it doesn't validate this migration file directly
  or its constraints): modeled on `tests/migrations/test_0020_session_model_selections_migration.py`.
  Covers: columns and types exist, `observed_at` defaults to `NOW()` on an insert that omits it, the
  index exists, the `NULLS NOT DISTINCT` unique constraint actually rejects a true duplicate and
  accepts two different-`NULL`-dimension rows are treated as conflicting (not distinct), `grafana_ro`
  has `SELECT` on the table, and re-running the migration (`CREATE TABLE IF NOT EXISTS`) is a no-op.
- `tests/migrations/test_init_sql_model_parity.py`: run as-is to confirm `KgStatModel` is picked up
  automatically once it exists on `Base`.
- `tests/test_brainstem/test_kg_stats_projection_schedule.py` (new, mirrors the shape of
  `test_freshness_review_schedule.py` rather than editing that file — this is a genuinely separate
  job with its own schedule now, not an extension of the weekly one): parser/cadence tests for the
  new daily cron, plus a scheduler-level test that `_last_kg_stats_projection_date` advances once per
  calendar day and re-fires correctly across a day boundary. **`test_freshness_review_schedule.py`
  itself is left untouched** — the weekly job's schedule and semantics did not change (codex finding
  2.1's concern about needing to touch the weekly event/consumer/fingerprint contract no longer
  applies, precisely because this plan no longer touches that contract).
- **AC-2 test** — `tests/test_memory/test_kg_stats_aggregate_integration.py::test_top_heat_entity_excludes_never_read`
  (`@pytest.mark.integration`, seeded Neo4j test stack): seeds several `access_count=0` entities
  alongside accessed ones, asserts none appear in `top_heat_entity` results (AC-2a). AC-2b (the two
  top-10 lists "reproduced independently agree") is a **handoff-time live verification**, not a unit
  test with a mock: a small script, `scripts/verify_fre1210_ac2.py`, that (1) reads the latest run's
  `top_heat_entity` rows from `kg_stats`, (2) re-runs the same Cypher query live against Neo4j at
  verification time, (3) asserts the two orderings match. Run it once against real data at Step 9 and
  paste its output into the ticket handoff comment — this is what "reproduced independently" means
  when the same query is the only correct implementation (two *differently-derived* computations of
  a ranking that must agree isn't meaningful here the way it is for, say, an aggregate count computed
  two structurally different ways).
- **AC-3 test** — `tests/test_memory/test_kg_stats_cold_mass_can_move.py::test_accessing_cold_entity_decrements_never_read_count`
  (`@pytest.mark.integration`, seeded Neo4j+Postgres test stack): create an entity with
  `access_count=0`, run `aggregate_kg_stats` + `write_kg_stats`, assert the written `cold_mass_ratio`
  reflects it as never-read; simulate an access (bump `access_count`, set `last_accessed_at`), re-run,
  assert the new `cold_mass_ratio` row is strictly lower than the previous one.

## T6.2 — The KG Health dashboard

**Governed by the `create-visualization` skill — invoke it, do not hand-author JSON.** Eleven panels
(ten from the ticket's own list, plus the `top_heat_entity` ranking panel AC-2 requires — see above)
on `pg-ledger`, tagged `grafana-native`, file `config/grafana/dashboards/kg_health.json`.

| # | Panel | Type | Query shape (against `kg_stats`) |
|---|---|---|---|
| 1 | Cold-mass trend | `timeseries`, unit `percentunit`, threshold step at 0.3 | `metric_name='cold_mass_ratio'` over `observed_at` |
| 2 | Heat histogram | `bargauge` | latest run, `metric_name='access_count_bucket'`, one bar per `dimension` |
| 3 | Recency × frequency heatmap | `table` + Grouping-to-matrix transform + colored-background cells (see below) | latest run, `metric_name='recency_frequency_cell'` (new combined metric, see below) |
| 4 | Type × recency heatmap | `table` + Grouping-to-matrix transform + colored-background cells (see below) | latest run, `metric_name='type_recency_cell'` (new combined metric, see below) |
| 5 | Node counts by type over time | `timeseries` | `metric_name='entity_count'`, series per `dimension` |
| 6 | Edge counts by type over time | `timeseries` | `metric_name='relationship_count'`, series per `dimension` |
| 7 | Embedding reachability trend | `timeseries` | `metric_name='embedding_missing'` over time |
| 8 | Duplicate-group / type-disagreement counts | `stat` ×2 or `table` | latest run, `metric_name IN ('duplicate_group_count','type_disagreement_count')` |
| 9 | Turns-without-entities rate | `timeseries`, unit `percentunit` | `metric_name='turns_without_entities_ratio'` over time |
| 10 | Growth per active day | `barchart` | day-over-day delta of summed `entity_count` |
| 11 | **Top-10 heat-ranked entities** (new — AC-2) | `table` | latest run, `metric_name='top_heat_entity'`, columns `dimension AS entity, metric_value AS score` |

### Closing the heatmap storage gap (was left open in the first draft — codex finding 3, "the plan
cannot claim AC-4 planned to completion while this remains unresolved")

`kg_stats` stores one `dimension` per row — it cannot natively express a 2D cross-tab. **Storage
decision, made now rather than deferred**: add two new metrics to `aggregate_kg_stats`, each row one
cross-tab cell, with a **pipe-joined combined `dimension`**:

```cypher
-- recency_frequency_cell: joint distribution of (recency_bucket, access_count_bucket)
-- computed from the same paged entity scan as items 3-4 above, cross-tabulated in Python
-- rather than a second Neo4j round-trip; dimension = "<recency_bucket>|<access_bucket>"

-- type_recency_cell: joint distribution of (entity_type, recency_bucket)
-- dimension = "<entity_type>|<recency_bucket>"
```

At the **Grafana query layer** (not in Postgres storage), split the combined dimension into two real
columns before Grafana ever sees it — this is exactly codex's fix direction for option (a): *"split
stored cell dimensions in SQL and pivot them into a supported frame; do not pass the joined string
directly."*
```sql
SELECT split_part(dimension, '|', 1) AS bucket_x,
       split_part(dimension, '|', 2) AS bucket_y,
       metric_value AS cnt
FROM kg_stats
WHERE metric_name = 'recency_frequency_cell' AND observed_at = (SELECT max(observed_at) FROM kg_stats WHERE metric_name = 'recency_frequency_cell')
```
This produces a genuine 3-column Table-format frame (`bucket_x`, `bucket_y`, `cnt`).

**Resolved (2026-08-11, docs-first per the skill) — panels 3–4 use `table`, not `heatmap`.** Read
live: the Grafana v13.1 heatmap panel docs
(https://grafana.com/docs/grafana/v13.1/panels-visualizations/visualizations/heatmap/) describe the
X-bucket setting as "how the x-axis is split into buckets... a time interval in the Size input" — no
categorical-axis mode is documented. The data-plane HeatmapCells contract
(https://grafana.com/developers/dataplane/heatmap) states cell size is "defined by the columns...
chosen as the xMax|xMin|x and the yMax|yMin|y" and its own worked example types `x` as **Time** and
`y` as **Number** — neither axis in `recency_frequency_cell` (recency bucket × access bucket) or
`type_recency_cell` (entity type × recency bucket) is a genuine timestamp, so the native `heatmap`
panel's documented contract doesn't fit either cross-tab. Instead: a `table` panel with the
**"Grouping to matrix" transform** (Grafana v13.1 transform docs — Column/Row/Cell-value fields,
`https://grafana.com/docs/grafana/v13.1/panels-visualizations/query-transform-data/transform-data/`)
reshapes the 3-column `(bucket_x, bucket_y, cnt)` frame into a wide grid, and the table panel's
documented **Colored background** cell display mode ("If thresholds, value mappings, or color
schemes are set, the cell background is displayed in the appropriate color") renders that grid as a
heatmap-style color grade. This is Grafana's own documented tool for a categorical×categorical
matrix, not an improvised workaround.

### Dashboard description caveats (AC-5, and codex finding 4.3's "near-empty" scoping fix)

The original draft said panels 1–4 would be near-empty at merge. **Fixed**: only genuine *trend*
panels need history to populate — 1 (cold-mass trend), 5 (node counts over time), 6 (edge counts over
time), 7 (embedding reachability trend), 9 (turns-without-entities rate), 10 (growth per active day,
which is inherently a delta and needs ≥2 runs). Panels 2, 3, 4, 8, and 11 all query the **latest**
run only and populate fully after the first projection completes. State this distinction in the
dashboard description, not the original blanket claim.

Also required by AC-5: state that access-over-time (day×hour) is not shown because Neo4j retains
only the last access — the history is discarded at write time, not merely unqueried, and no panel
here implies otherwise.

### Follow the `create-visualization` skill's Arm A exactly for the build

Ephemeral Editor instance → scratch dashboard → per-panel: pick viz type first, set query, set
`fieldConfig.defaults.unit` + thresholds (bargauge/stat panels need thresholds per Gate 0) → extract
via `GET /api/dashboards/uid/<uid>` (never Settings → JSON Model, wrong schema on 13.1.3) → commit →
tear down → verify against a throwaway worktree-mounted Viewer instance (this worktree is `build2`,
not the primary `/opt/seshat` checkout the shared `cloud-sim-grafana` mounts — stand up the second
throwaway instance per the skill's explicit worktree warning).

Run Gate 0 (`jq` field-config gate), the Grafana render-assert, and the AC-4 Playwright screenshot +
Cypher bucket cross-check (`scripts/verify_fre1210_heatmap_buckets.py`, comparing the rendered
heatmap's cell counts against a fresh Cypher computation of the same joint distribution) before
calling T6.2 done. Save the AC-4 screenshot artifact path and command output in the ticket handoff.

## Risk tier and review

**Standard/Complex** — new Postgres schema (migration + model), a new scheduled job, new Cypher
aggregation logic, and a Grafana dashboard build. Codex plan-review completed (`task-msoo2kgh-hf3a14`,
2026-08-11) and every finding folded into this revision. Owner approval next, before implementation.

## Test commands

```bash
make test-file FILE=tests/test_memory/test_kg_stats_aggregate.py
make test-file FILE=tests/test_memory/test_kg_stats_write.py
make test-file FILE=tests/migrations/test_0026_kg_stats_migration.py
make test-file FILE=tests/migrations/test_init_sql_model_parity.py
make test-file FILE=tests/test_brainstem/test_kg_stats_projection_schedule.py
make test                  # full suite
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```
