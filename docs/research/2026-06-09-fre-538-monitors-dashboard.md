# FRE-538 (C3) — Monitors Dashboard: Joinability-Audit + SLM-Health Surfacing

> **Date:** 2026-06-09 · **Ticket:** FRE-538 (Tier-2:Sonnet) · **Project:** Telemetry Surface Audit
> **Refs:** ADR-0074 (joinability probe) · ADR-0083 / FRE-399 (SLM health probe) · A1 (FRE-533 reconciliation) · A2 (FRE-534 templates, added `monitors-slm-health-index-template.json`) · C1/C2 (FRE-536/537 — the dashboard pattern this mirrors)
> **Artifact:** `config/kibana/dashboards/monitors_joinability_slm.ndjson` · **Test:** `tests/scripts/test_monitors_dashboard.py`

Two monitor index families wrote data for weeks but had **zero** visualizations. This surfaces both as one
legacy-aggs Kibana dashboard. The work was measure-first: every field was walked through live `_field_caps`
**and** per-index `_mapping` before any panel was wired, which is what caught the decisive trap below.

---

## The decisive finding — the SLM-health mapping straddle

`agent-monitors-slm-health-*` is **straddled** across its daily indices:

| Field | Historical indices (06.02–06.08, dynamic-mapped) | New index (06.09, post-FRE-534 template) | Aggregatable across all? |
|---|---|---|---|
| `status` | `text` + `status.keyword` | bare `keyword` (no `.keyword`) | **NO** |
| `error`  | `text` + `error.keyword`  | bare `text` (no `.keyword`)   | **NO** (text) |
| `reachable` | `boolean` | `boolean` | **YES** |
| `probe_latency_ms` | `float` | `float` | **YES** |

ES index templates apply only to **newly created** indices, so the historical SLM indices keep the old
dynamic mapping. The consequence is a field-name fork with no complete choice:

```
terms(status.keyword)  -> up 1598 / down 186     (historical indices only; misses 06.09)
terms(status)          -> down 54                (06.09 only; misses everything before)
terms(reachable)       -> true 1598 / false 240  (ALL indices; 186 + 54 == 240 — reconciles exactly)
```

**Design choice:** aggregate availability on `reachable` (boolean, consistent), latency on `probe_latency_ms`
(float, consistent). `status`/`error` are surfaced **only as `_source` columns in a saved search** — Discover
renders `_source` text regardless of the mapping straddle, so error-reason hints appear without an aggregation.
A static test (`test_aggregations_only_use_straddle_safe_fields`) pins every aggregation to the verified-safe
set so a future edit can't silently reintroduce a `status`/`.keyword` agg.

This is the same failure class A1 (FRE-533) found — a `.keyword`-on-bare-keyword agg-to-nothing — except here
it is *direction-dependent*: the bare field is correct on new data and wrong on old, and vice-versa.

## Joinability — clean top-level, nested detail deferred

`agent-monitors-joinability-*` is consistent (the template `dynamic:false` was present from the first index,
05.23): `outcome` and `source` are bare `keyword` everywhere; `started_at` is the time field (**no `@timestamp`**
on these docs); `duration_ms` is `float`.

The orphan halt-signal does **not** require touching the nested fields. The top-level `outcome` is the
run-level verdict and reconciles with the nested orphan data:

```
nested orphans.severity   -> yellow 454 / red 140
top-level outcome         -> green 271 / red 140 / yellow 42 / skipped 33
=> outcome:(red OR yellow) == "orphans > 0"  (the ADR-0074 halt condition)
```

`orphans` and `substrate_checks` are mapped `nested`; **legacy Kibana aggregation visualizations have no
nested bucket-agg path** (only Lens / raw ES do). So the per-substrate breakdown is **deferred** to a
follow-up (Lens nested support, or a flattened per-substrate ES projection) rather than faked — the same
measure-first/don't-fake move as FRE-536→547 and FRE-537→548.

---

## Panels (one dashboard, `Monitors — Joinability & SLM Health`)

Two self-contained index-patterns defined inline in the NDJSON (`agent-monitors-joinability-pattern` /
`agent-monitors-slm-health-pattern`), each with its own `timeFieldName`. No edit to `data_views.ndjson`.

| # | Title | Type | Field(s) | Maps to ticket candidate |
|---|---|---|---|---|
| J1 | Joinability Outcome Over Time | stacked area | `started_at` × `outcome` | orphan count over time (red/yellow bands) |
| J2 | Joinability Outcome Distribution | donut | `outcome` | join-success rate |
| J3 | Runs With Orphans Detected | metric | count `outcome:(red OR yellow)` | **halt condition orphans>0 at a glance** |
| J4 | Joinability Probe Duration Over Time | line | avg `duration_ms` × `started_at` | probe operational health |
| S1 | SLM Reachability Over Time | area | `probed_at` × `reachable` | availability over time (false band = down) |
| S2 | SLM Availability Split | donut | `reachable` | reachability summary |
| S3 | SLM Probe Latency Over Time | line | avg `probe_latency_ms` × `probed_at` | latency to SLM |
| S4 | Recent Unreachable SLM Probes | saved search | `reachable:false`; cols `status, error, probe_latency_ms, trace_id` | error-reason hints (straddle-safe) |

## Import + per-panel aggregation proof (local Kibana :5601 / ES :9200 — not a deploy)

```
./config/kibana/import_dashboards.sh
  OK    monitors_joinability_slm.ndjson

J1/J2  date_histogram(started_at) × terms(outcome): 18 day buckets; outcomes populate
J3     outcome:(red OR yellow): 182 runs with orphans
J4     avg(duration_ms): 89.7 ms
S1/S2  terms(reachable): true 1598 / false 245
S3     avg(probe_latency_ms): 293.35 ms
S4     reachable:false: 245 unreachable probe docs
```

> **Note (pre-existing, not introduced here):** `prompt-cost-cache.ndjson` fails to import on the local Kibana
> with a `strict_dynamic_mapping_exception` on its Lens objects (`migrationVersion`/`references`). That file is
> unchanged by this ticket (zero diff) and is a separate Kibana-saved-objects/Lens issue **already tracked as
> FRE-546** ("prompt-cost-cache dashboard fails Kibana import (stale saved object)"); out of scope for C3.

## Acceptance (FRE-538) — met

- ✅ Both monitor families have a populated dashboard (J1–J4 + S1–S4; per-panel agg proof above).
- ✅ Orphan halt condition (`outcome:(red OR yellow)` == orphans>0) visible at a glance (J3 metric + J1 bands).
- ✅ Fields verified mapped/aggregatable before wiring; straddle avoided (`reachable`/`probe_latency_ms`, never
     `status`); test-enforced.
- ✅ Exported to version-controlled NDJSON, registered in `import_dashboards.sh`, imports clean.
- ✅ Per-substrate nested breakdown deferred as a Needs-Approval follow-up (not faked).

## Deferred (not faked)

- Per-substrate joinability breakdown (nested `orphans`/`substrate_checks`) — legacy aggs can't; follow-up ticket.
- `status`/`error` term aggregations — straddled mapping; surfaced via saved-search `_source` instead.
- Rich SLM fields (`gpu_util_pct`/`vram_*`/`queue_depth`/`latency_ema_ms`) — null in current down-probe data;
  revisit when the SLM is reachable.
- `prompt-cost-cache.ndjson` Lens import failure on local Kibana — pre-existing, already tracked as FRE-546.
