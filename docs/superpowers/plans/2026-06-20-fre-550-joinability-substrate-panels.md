# FRE-550: Per-Substrate Joinability Breakdown Panels

**Date:** 2026-06-20  
**Ticket:** FRE-550 (Tier-2:Sonnet)  
**Project:** Telemetry Surface Audit  
**Refs:** FRE-538 (C3, parent dashboard) · ADR-0074 (joinability probe) · FRE-546 (legacy-aggs preference)

---

## Decision: Option 2 — Flattened per-substrate ES projection

Legacy Kibana agg vizzes cannot aggregate `nested` fields (`orphans`, `substrate_checks`).

**Option 1 (Lens nested agg) is rejected** because:
- C-series dashboards use legacy agg format; a Lens carve-out breaks the convention and the existing `test_monitors_dashboard.py` which validates `visState` JSON (legacy agg schema only).
- FRE-546 documented that Lens saved-object NDJSON has format compatibility issues in this project's Kibana.

**Option 2 (flat projection)** is chosen:
- Probe emits one additional flat doc per `(run_id, substrate)` into `agent-monitors-joinability-substrate-*`.
- Top-level keyword fields → legacy agg terms work cleanly.
- Unit-testable factory function.
- No new setting — substrate prefix derives as `f"{joinability_probe_index_prefix}-substrate"`.

---

## Files changed

| File | Action |
|------|--------|
| `src/personal_agent/observability/joinability/result.py` | Add `SubstrateResultDoc` model + factory |
| `src/personal_agent/observability/joinability/sink.py` | Add `write_substrate_results()` |
| `docker/elasticsearch/monitors-joinability-substrate-index-template.json` | New flat-substrate ES template |
| `scripts/setup-elasticsearch.sh` | Register new template |
| `scripts/monitors/joinability_probe.py` | Emit substrate docs after run doc |
| `config/kibana/dashboards/monitors_joinability_slm.ndjson` | New index-pattern + 3 panels added to existing dashboard |
| `tests/observability/test_substrate_result.py` | New — factory unit tests (8 cases) |
| `tests/observability/test_substrate_sink.py` | New — sink unit tests (stub ES) |
| `tests/scripts/test_monitors_dashboard.py` | Pattern-scoped agg guard + counts + template-priority/float-mapping static checks |

---

## Step-by-step implementation

### Step 1 — `SubstrateResultDoc` model + factory (`result.py`)

Add below the existing `ResultDoc` class:

```python
class SubstrateResultDoc(BaseModel):
    """Flat per-(run, substrate) doc written to agent-monitors-joinability-substrate-*.

    Flattened projection of one SubstrateCheck + its matching Orphans from a
    ResultDoc run. Enables legacy Kibana agg terms on substrate/status/severity
    without nested-field support (ADR-0074 / FRE-550).

    Attributes:
        run_id: Parent ResultDoc.run_id — FK for joining back to the run.
        started_at: Copied from parent (time field for the index-pattern).
        substrate: E.g. "postgres.sessions", "elasticsearch.agent_logs".
        status: Check status: "green" / "yellow" / "red" / "skipped".
        expected: "required" / "conditional" / "absent_ok".
        observed_count: Rows/docs/nodes found for this substrate.
        duration_ms: Wall-clock time for this substrate walk.
        error: Error string if the substrate was unreachable.
        orphan_count: Total orphans attributed to this substrate.
        orphan_red_count: Hard-violation orphans.
        orphan_yellow_count: Soft-violation orphans.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    started_at: datetime
    substrate: str
    status: Literal["green", "yellow", "red", "skipped"]
    expected: Literal["required", "conditional", "absent_ok"]
    observed_count: int
    duration_ms: float
    error: str | None = None
    orphan_count: int = 0
    orphan_red_count: int = 0
    orphan_yellow_count: int = 0


def substrate_docs_from_result(result: ResultDoc) -> list[SubstrateResultDoc]:
    """Flatten a ResultDoc into one SubstrateResultDoc per substrate check.

    Matches orphans to their substrate by the ``orphan.substrate`` field.
    Orphans whose ``substrate`` is not among ``result.substrate_checks`` are
    silently dropped (defensive — every orphan-emitting walk also appends a
    check for the same substrate, so this should not occur in practice; the
    behaviour is pinned by a test).

    The walk appends exactly one ``SubstrateCheck`` per substrate (each
    ``_walk_*`` method emits one check with a fixed substrate string), so
    substrate values are unique within a run. The sink derives the ES doc id
    ``{run_id}::{substrate}`` from this, so a duplicate substrate would
    silently overwrite a sibling doc. Rather than allow that, the factory
    **enforces** the invariant: a duplicate ``SubstrateCheck.substrate`` raises
    ``ValueError`` loudly here (the single chokepoint) instead of corrupting ES
    downstream. This converts a future walk regression into a failed probe run
    rather than missing dashboard rows.

    Args:
        result: Completed run-level result document.

    Returns:
        One flat doc per SubstrateCheck in ``result.substrate_checks`` (order
        preserved).

    Raises:
        ValueError: If two substrate checks share a ``substrate`` value (would
            collide on the ``{run_id}::{substrate}`` ES doc id).
    """
    substrates = [c.substrate for c in result.substrate_checks]
    if len(substrates) != len(set(substrates)):
        dupes = sorted({s for s in substrates if substrates.count(s) > 1})
        raise ValueError(
            f"duplicate substrate(s) in result {result.run_id}: {dupes} — "
            f"would collide on the substrate-doc id"
        )

    orphans_by_substrate: dict[str, list[Orphan]] = {}
    for orphan in result.orphans:
        orphans_by_substrate.setdefault(orphan.substrate, []).append(orphan)

    docs: list[SubstrateResultDoc] = []
    for check in result.substrate_checks:
        sub_orphans = orphans_by_substrate.get(check.substrate, [])
        docs.append(
            SubstrateResultDoc(
                run_id=result.run_id,
                started_at=result.started_at,
                substrate=check.substrate,
                status=check.status,
                expected=check.expected,
                observed_count=check.observed_count,
                duration_ms=check.duration_ms,
                error=check.error,
                orphan_count=len(sub_orphans),
                orphan_red_count=sum(1 for o in sub_orphans if o.severity == "red"),
                orphan_yellow_count=sum(1 for o in sub_orphans if o.severity == "yellow"),
            )
        )
    return docs
```

### Step 2 — `write_substrate_results()` + `substrate_index_name_for()` in `sink.py`

The run-level sink already has an `index_name_for(doc, *, prefix)` helper. Add a matching
substrate helper so index naming stays testable and consistent, then the writer uses it.

The `trace_id` is **passed in by the caller** (the probe's `ctx.trace_id`) — `SubstrateResultDoc`
deliberately does not store the probe's own trace (it carries `run_id`, the join key back to the
run doc). This keeps the structured-log `trace_id` honest (the probe's real `SystemTraceContext`
trace) rather than overloading `run_id` into the `trace_id` slot.

```python
def substrate_index_name_for(doc: "SubstrateResultDoc", *, prefix: str) -> str:
    """Compute the daily index name for a flat substrate doc.

    Args:
        doc: Flat per-substrate result document.
        prefix: Base joinability prefix (e.g. ``agent-monitors-joinability``);
            the substrate suffix is appended here so the run-doc and
            substrate-doc indices share one settings key.

    Returns:
        ``{prefix}-substrate-YYYY.MM.DD`` (UTC date from ``started_at``).
    """
    return f"{prefix}-substrate-{doc.started_at.strftime('%Y.%m.%d')}"


async def write_substrate_results(
    es: "AsyncElasticsearch",
    docs: "Sequence[SubstrateResultDoc]",
    *,
    prefix: str,
    trace_id: str,
) -> None:
    """Write flat per-substrate docs to ES (one per (run, substrate)).

    Document id: ``{run_id}::{substrate}`` (deterministic + idempotent, so a
    re-run of the same probe overwrites rather than duplicates).

    Args:
        es: Connected AsyncElasticsearch client.
        docs: Flat substrate docs from ``substrate_docs_from_result()``.
        prefix: Base index prefix (e.g. ``agent-monitors-joinability``).
        trace_id: The probe run's ``SystemTraceContext`` trace id, for the
            structured completion log (the docs themselves carry ``run_id``).

    Raises:
        elasticsearch.ApiError: When an index operation fails. The caller logs
            and swallows — a substrate-doc write failure must not abort the
            brainstem scheduler loop (mirrors ``write_result`` contract).
    """
    for doc in docs:
        await es.index(
            index=substrate_index_name_for(doc, prefix=prefix),
            id=f"{doc.run_id}::{doc.substrate}",
            document=doc.model_dump(mode="json"),
        )
    log.info(
        "joinability_substrate_docs_indexed",
        count=len(docs),
        run_id=docs[0].run_id if docs else None,
        trace_id=trace_id,
    )
```

Imports to add in `sink.py`: `from collections.abc import Sequence` (under `from __future__`),
and add `SubstrateResultDoc` to the existing runtime import line
`from personal_agent.observability.joinability.result import ResultDoc` →
`... import ResultDoc, SubstrateResultDoc` (matches how `ResultDoc` is already imported, even
though both are used only in annotations).

### Step 3 — ES index template

`docker/elasticsearch/monitors-joinability-substrate-index-template.json`:

**BLOCKER fix (codex):** `agent-monitors-joinability-substrate-*` is a strict subset of the
existing `agent-monitors-joinability-*` pattern (`priority:100`, `dynamic:false`). ES picks the
single highest-priority matching template — a tie at `100` is a registration error, and if the
broad template won it would apply `dynamic:false` with **no** substrate-field properties, silently
dropping `status`/`substrate`/`orphan_count`/`duration_ms`. The substrate template therefore uses
`priority:200` so it strictly outranks the parent for the `-substrate-*` indices. (A static test
asserts `200 > 100`.)

```json
{
  "index_patterns": ["agent-monitors-joinability-substrate-*"],
  "priority": 200,
  "template": {
    "settings": {
      "number_of_shards": 1,
      "number_of_replicas": 0,
      "index.codec": "best_compression",
      "index.refresh_interval": "10s",
      "index.lifecycle.name": "agent-monitors-joinability-policy"
    },
    "mappings": {
      "dynamic": false,
      "properties": {
        "run_id":             { "type": "keyword" },
        "started_at":         { "type": "date" },
        "substrate":          { "type": "keyword" },
        "status":             { "type": "keyword" },
        "expected":           { "type": "keyword" },
        "observed_count":     { "type": "long" },
        "duration_ms":        { "type": "float" },
        "error":              { "type": "text" },
        "orphan_count":       { "type": "long" },
        "orphan_red_count":   { "type": "long" },
        "orphan_yellow_count":{ "type": "long" }
      }
    }
  },
  "_meta": {
    "description": "Per-substrate joinability probe flat projection (FRE-550 / ADR-0074). One doc per (run, substrate).",
    "managed_by": "scripts/setup-elasticsearch.sh"
  }
}
```

### Step 4 — `setup-elasticsearch.sh` registration

After the existing joinability template block, add:

```bash
put_resource "Index template: agent-monitors-joinability-substrate-template" \
  "/_index_template/agent-monitors-joinability-substrate-template" \
  "$PROJECT_ROOT/docker/elasticsearch/monitors-joinability-substrate-index-template.json"
```

The substrate index reuses the existing `agent-monitors-joinability-policy` ILM policy (same 7d/180d lifecycle — ~same doc volume).

### Step 5 — Probe script update (`joinability_probe.py`)

After the existing `write_result()` call in `_run()`, add:

```python
from personal_agent.observability.joinability.result import substrate_docs_from_result
from personal_agent.observability.joinability.sink import write_substrate_results

# After the existing write_result() block, still inside `if write_es and es is not None:`
# (reuse the same guard rather than re-testing — keeps the two writes atomic w.r.t. the flag):
    sub_docs = substrate_docs_from_result(doc)
    if sub_docs:
        try:
            await write_substrate_results(
                es,
                sub_docs,
                prefix=settings.joinability_probe_index_prefix,
                trace_id=ctx.trace_id,
            )
        except Exception as exc:  # noqa: BLE001 — sink failure is logged, not fatal
            log.warning(
                "joinability_probe_substrate_es_write_failed",
                error=str(exc),
                trace_id=ctx.trace_id,
            )
```

### Step 6 — Kibana NDJSON additions to `monitors_joinability_slm.ndjson`

Add (appended before the final dashboard object):

**New index-pattern** (line before dashboard):
```json
{"attributes": {"allowHidden": false, "fieldAttrs": "{}", "fieldFormatMap": "{}", "fields": "[]", "name": "agent-monitors-joinability-substrate-*", "runtimeFieldMap": "{}", "sourceFilters": "[]", "timeFieldName": "started_at", "title": "agent-monitors-joinability-substrate-*"}, "coreMigrationVersion": "8.8.0", "created_at": "2026-06-20T00:00:00.000Z", "id": "agent-monitors-joinability-substrate-pattern", "managed": false, "references": [], "type": "index-pattern", "typeMigrationVersion": "8.0.0", "version": "1"}
```

**3 new visualizations:**

`mon-join-sub-status` — "Per-Substrate Check Status" (horizontal bar: terms(substrate) × terms(status)):
```json
{"attributes": {"description": "Per-substrate check status breakdown — which substrates fail most often.", "kibanaSavedObjectMeta": {"searchSourceJSON": "{\"query\": {\"language\": \"kuery\", \"query\": \"\"}, \"filter\": [], \"indexRefName\": \"kibanaSavedObjectMeta.searchSourceJSON.index\"}"}, "title": "Per-Substrate Check Status", "uiStateJSON": "{}", "version": 1, "visState": "{\"type\": \"histogram\", \"aggs\": [{\"id\": \"1\", \"enabled\": true, \"type\": \"count\", \"schema\": \"metric\", \"params\": {\"customLabel\": \"Probe runs\"}}, {\"id\": \"2\", \"enabled\": true, \"type\": \"terms\", \"schema\": \"segment\", \"params\": {\"field\": \"substrate\", \"size\": 20, \"order\": \"desc\", \"orderBy\": \"1\"}}, {\"id\": \"3\", \"enabled\": true, \"type\": \"terms\", \"schema\": \"group\", \"params\": {\"field\": \"status\", \"size\": 5, \"order\": \"desc\", \"orderBy\": \"1\"}}], \"params\": {\"addTooltip\": true, \"addLegend\": true, \"legendPosition\": \"right\", \"mode\": \"stacked\"}, \"title\": \"Per-Substrate Check Status\"}"}, "coreMigrationVersion": "8.8.0", "created_at": "2026-06-20T00:00:00.000Z", "id": "mon-join-sub-status", "managed": false, "references": [{"id": "agent-monitors-joinability-substrate-pattern", "name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern"}], "type": "visualization", "typeMigrationVersion": "8.5.0", "updated_at": "2026-06-20T00:00:00.000Z", "version": "1"}
```

`mon-join-sub-orphans` — "Orphan Counts by Substrate" (bar: terms(substrate), avg(orphan_red_count), avg(orphan_yellow_count)):
```json
{"attributes": {"description": "Average red + yellow orphan count per substrate per probe run.", "kibanaSavedObjectMeta": {"searchSourceJSON": "{\"query\": {\"language\": \"kuery\", \"query\": \"\"}, \"filter\": [], \"indexRefName\": \"kibanaSavedObjectMeta.searchSourceJSON.index\"}"}, "title": "Orphan Counts by Substrate", "uiStateJSON": "{}", "version": 1, "visState": "{\"type\": \"histogram\", \"aggs\": [{\"id\": \"1\", \"enabled\": true, \"type\": \"avg\", \"schema\": \"metric\", \"params\": {\"field\": \"orphan_red_count\", \"customLabel\": \"Avg red orphans\"}}, {\"id\": \"2\", \"enabled\": true, \"type\": \"avg\", \"schema\": \"metric\", \"params\": {\"field\": \"orphan_yellow_count\", \"customLabel\": \"Avg yellow orphans\"}}, {\"id\": \"3\", \"enabled\": true, \"type\": \"terms\", \"schema\": \"segment\", \"params\": {\"field\": \"substrate\", \"size\": 20, \"order\": \"desc\", \"orderBy\": \"1\"}}], \"params\": {\"addTooltip\": true, \"addLegend\": true, \"legendPosition\": \"right\", \"mode\": \"stacked\"}, \"title\": \"Orphan Counts by Substrate\"}"}, "coreMigrationVersion": "8.8.0", "created_at": "2026-06-20T00:00:00.000Z", "id": "mon-join-sub-orphans", "managed": false, "references": [{"id": "agent-monitors-joinability-substrate-pattern", "name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern"}], "type": "visualization", "typeMigrationVersion": "8.5.0", "updated_at": "2026-06-20T00:00:00.000Z", "version": "1"}
```

`mon-join-sub-duration` — "Per-Substrate Walk Duration" (bar: terms(substrate), avg(duration_ms)):
```json
{"attributes": {"description": "Average walk duration per substrate — latency hotspot detector.", "kibanaSavedObjectMeta": {"searchSourceJSON": "{\"query\": {\"language\": \"kuery\", \"query\": \"\"}, \"filter\": [], \"indexRefName\": \"kibanaSavedObjectMeta.searchSourceJSON.index\"}"}, "title": "Per-Substrate Walk Duration", "uiStateJSON": "{}", "version": 1, "visState": "{\"type\": \"histogram\", \"aggs\": [{\"id\": \"1\", \"enabled\": true, \"type\": \"avg\", \"schema\": \"metric\", \"params\": {\"field\": \"duration_ms\", \"customLabel\": \"Avg duration (ms)\"}}, {\"id\": \"2\", \"enabled\": true, \"type\": \"terms\", \"schema\": \"segment\", \"params\": {\"field\": \"substrate\", \"size\": 20, \"order\": \"desc\", \"orderBy\": \"1\"}}], \"params\": {\"addTooltip\": true, \"addLegend\": true, \"legendPosition\": \"right\"}, \"title\": \"Per-Substrate Walk Duration\"}"}, "coreMigrationVersion": "8.8.0", "created_at": "2026-06-20T00:00:00.000Z", "id": "mon-join-sub-duration", "managed": false, "references": [{"id": "agent-monitors-joinability-substrate-pattern", "name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern"}], "type": "visualization", "typeMigrationVersion": "8.5.0", "updated_at": "2026-06-20T00:00:00.000Z", "version": "1"}
```

**Updated dashboard object** (add 3 panels to `panelsJSON`, add 3 refs):
- Panels at y=60: sub-status (x=0,w=48,h=15), sub-orphans (x=0,y=75,w=24,h=15), sub-duration (x=24,y=75,w=24,h=15)
- References: panel_8→mon-join-sub-status, panel_9→mon-join-sub-orphans, panel_10→mon-join-sub-duration

### Step 7 — Test suite updates

**`tests/scripts/test_monitors_dashboard.py`** — the guard must become **index-pattern-scoped**.

Codex blocker: adding `status` to the flat global `SAFE_AGG_FIELDS` would let an SLM-pattern panel
aggregate on the straddled `status` field undetected. Replace the single flat set with a per-pattern
map, and resolve each viz's pattern from its own index-pattern reference before checking its agg
fields. `status` is then safe **only** under the substrate pattern.

```python
# 1. Add the new pattern to EXPECTED_INDEX_PATTERNS:
EXPECTED_INDEX_PATTERNS = {
    "agent-monitors-joinability-pattern": "started_at",
    "agent-monitors-slm-health-pattern": "probed_at",
    "agent-monitors-joinability-substrate-pattern": "started_at",   # FRE-550
}

# 2. Replace flat SAFE_AGG_FIELDS with a per-pattern map (status is ONLY safe
#    for the substrate index, never the straddled SLM index):
SAFE_AGG_FIELDS_BY_PATTERN: dict[str, frozenset[str]] = {
    "agent-monitors-joinability-pattern": frozenset(
        {"outcome", "source", "duration_ms", "started_at"}
    ),
    "agent-monitors-slm-health-pattern": frozenset(
        {"reachable", "probe_latency_ms", "probed_at"}
    ),
    # FRE-550 flat projection: top-level keyword/long/float fields, all
    # consistently mapped from index creation (priority-200 template,
    # dynamic:false with explicit properties). status is straddle-safe HERE
    # because this index has a single bare-keyword status mapping.
    "agent-monitors-joinability-substrate-pattern": frozenset(
        {"substrate", "status", "expected", "duration_ms", "started_at",
         "observed_count", "orphan_count", "orphan_red_count", "orphan_yellow_count"}
    ),
}

# 3. Rewrite test_aggregations_only_use_straddle_safe_fields to resolve each
#    viz's pattern from its references, then check its agg fields against that
#    pattern's safe set:
def _pattern_of(viz: dict) -> str:
    refs = [r["id"] for r in viz["references"] if r["type"] == "index-pattern"]
    assert len(refs) == 1, f"{viz['id']} must reference exactly one index-pattern, got {refs}"
    return refs[0]

def test_aggregations_only_use_straddle_safe_fields() -> None:
    for viz in _by_type(_objects(), "visualization"):
        pattern = _pattern_of(viz)
        safe = SAFE_AGG_FIELDS_BY_PATTERN[pattern]
        for field in _agg_fields(viz):
            assert not field.endswith(".keyword"), (
                f"{viz['id']} aggregates on {field!r}; a ``.keyword`` agg is the straddle/A1 trap"
            )
            assert field in safe, (
                f"{viz['id']} (pattern {pattern}) aggregates on {field!r}, not in its "
                f"straddle-safe set {sorted(safe)}"
            )
```

```python
# 4. Update counts in test_ndjson_is_valid_and_has_one_dashboard:
assert len(_by_type(objs, "visualization")) == 10   # was 7, +3 substrate panels
# (test_every_panel_references_a_monitor_index_pattern already passes once the
#  new substrate pattern is in EXPECTED_INDEX_PATTERNS — it checks membership.)
```

**New static template tests** — added to `tests/scripts/test_monitors_dashboard.py` (it already
reads repo files statically; the two template JSONs are loaded via `REPO_ROOT`). Pin the two
mapping traps codex flagged:
- `test_substrate_template_duration_ms_is_float` — `duration_ms` mapped `float` (not the long-trap
  default) in `docker/elasticsearch/monitors-joinability-substrate-index-template.json`.
- `test_substrate_template_outranks_parent` — substrate template `priority` (200) **strictly
  greater** than the parent `monitors-joinability-index-template.json` `priority` (100), so the
  parent's `dynamic:false` can't shadow the substrate fields.

**`tests/observability/test_substrate_result.py`** (new file — factory):
- `test_substrate_docs_from_result_empty` — ResultDoc with no substrate_checks → `[]`.
- `test_substrate_docs_one_per_check` — N checks → N docs, order preserved, run_id/started_at copied.
- `test_substrate_docs_match_orphans_by_substrate` — orphan_red_count/orphan_yellow_count tallied
  per substrate from `orphan.severity`.
- `test_substrate_docs_check_with_zero_orphans` — a checked substrate with **no** orphans emits a
  doc with all three orphan counts `0` (codex: the zero-orphan row must still be emitted).
- `test_substrate_docs_mixed_orphan_and_clean` — one substrate has orphans, another checked
  substrate has none; both rows present, counts correct per substrate (codex).
- `test_substrate_docs_orphan_substrate_not_in_checks_ignored` — orphan whose substrate has no
  matching check is silently dropped (no phantom doc).
- `test_substrate_docs_skipped_run` — skipped-outcome ResultDoc (no checks) → `[]`.
- `test_substrate_docs_substrate_uniqueness_holds_for_real_walk` — build a representative
  multi-substrate `ResultDoc` and assert the produced docs' `(run_id, substrate)` ids are unique
  (pins the doc-id no-collision invariant codex flagged).
- `test_substrate_docs_duplicate_substrate_raises` — a `ResultDoc` with two `SubstrateCheck`s
  sharing a `substrate` value raises `ValueError` from the factory (the enforced invariant — a
  duplicate would otherwise overwrite a sibling ES doc id).

**`tests/observability/test_substrate_sink.py`** (new file — sink, mirrors existing
`test_joinability_*` sink-style with a stub ES):
- `test_substrate_index_name_for` — `{prefix}-substrate-YYYY.MM.DD` from `started_at`.
- `test_write_substrate_results_indexes_each_doc` — stub ES records one `index()` per doc with the
  right index name, `id={run_id}::{substrate}`, and the `model_dump(mode="json")` body.
- `test_write_substrate_results_empty_noop` — empty docs → no `index()` calls, log run_id `None`.
- `test_write_substrate_results_error_propagates` — stub ES raising on `index()` propagates (the
  caller, not the sink, swallows — mirrors `write_result`).

### Step 8 — Quality gates

```
make test-file FILE=tests/observability/test_substrate_result.py   → all new tests pass
make test-file FILE=tests/scripts/test_monitors_dashboard.py       → updated counts + fields pass
make test                                                           → full suite green
make mypy                                                           → clean
make ruff-check && make ruff-format                                 → clean
pre-commit run --all-files                                          → clean
```

---

## Acceptance criteria

- [ ] Per-substrate orphan + check-status panels populated against `agent-monitors-joinability-substrate-*` data (verified live post-deploy).
- [ ] Approach (Option 2: flattened emit) recorded in this plan and in the Linear handoff comment.
- [ ] ES template + explicit field mappings added for all new fields (no float→long trap).
- [ ] Dashboard NDJSON exported with 3 new panels, imports clean via `import_dashboards.sh`.
- [ ] `make test` green, `make mypy` clean.

---

## Not in scope

- Backfilling substrate docs for historical run docs (probe writes substrate docs on next run).
- Retaining the run-level `substrate_checks` nested array (it stays — the flat projection is additive).
- Per-orphan-kind breakdown (kind field exists but adding it adds a third doc type; defer if needed).
