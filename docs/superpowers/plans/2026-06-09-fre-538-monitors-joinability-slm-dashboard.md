# FRE-538 (C3) — Monitors Dashboard: Joinability-Audit + SLM-Health Surfacing

> **Date:** 2026-06-09 · **Ticket:** FRE-538 (Tier-2:Sonnet) · **Project:** Telemetry Surface Audit
> **Refs:** ADR-0074 (joinability probe) · ADR-0083 / FRE-399 (cross-tunnel SLM health) · A1 (FRE-533 reconciliation) · A2 (FRE-534 templates, Done — added `monitors-slm-health-index-template.json`) · C1/C2 (FRE-536/537, the dashboard pattern this mirrors)
> **Pattern:** mirrors C1/C2 exactly — legacy aggs-based visualizations (not Lens, per FRE-546), one `*.ndjson` file registered in `import_dashboards.sh`, static-validation test guards the A1 keyword trap.

---

## Scope decision (measure-first; verified against live `_field_caps` + per-index `_mapping`)

Two monitor index families write data but have **zero** visualizations. I traced every candidate panel
through **emit doc → live ES mapping → aggregatability** before designing. The decisive finding is a
**straddle in the SLM-health family** (the exact failure mode the project warns about):

### SLM health (`agent-monitors-slm-health-*`) — STRADDLED

| Field | Old indices (06.02–06.08, dynamic-mapped) | New index (06.09, post-FRE-534 template) | Safe to aggregate? |
|---|---|---|---|
| `status` | `text` + `status.keyword` | bare `keyword` (no `.keyword`) | **NO** — neither `status` nor `status.keyword` covers all data |
| `error`  | `text` + `error.keyword`  | bare `text` (no `.keyword`)   | **NO** — text, not aggregatable |
| `reachable` | `boolean` | `boolean` | **YES** — consistent across all indices |
| `probe_latency_ms` | `float` | `float` | **YES** |
| `latency_ema_ms` / `queue_depth` | `float` / `integer` | `float` / `integer` | yes (but null in current data — down probes) |

> Templates apply only to **newly created** indices, so historical SLM indices keep their old dynamic
> mapping. `status.keyword` agg → catches old data only (up 1598 / down 186); bare `status` agg → catches
> the new index only (down 54). **Neither is complete.** `reachable` (boolean) is the join-safe availability
> signal: `true 1598 / false 240` across **all** indices — and `186 + 54 = 240`, so it reconciles exactly.
>
> **Design choice:** aggregate availability on **`reachable`** (boolean), latency on **`probe_latency_ms`**
> (float). Surface `status`/`error` only as **`_source` columns in a saved search** — Discover renders
> `_source` text regardless of the mapping straddle, so error-reason hints appear without an agg.

### Joinability (`agent-monitors-joinability-*`) — CLEAN top-level, NESTED detail

| Field | Mapping (old 05.23 == new) | Aggregatable? |
|---|---|---|
| `outcome` | bare `keyword` | **YES** — green 271 / red 140 / yellow 42 / skipped 33 |
| `source`  | bare `keyword` | **YES** — scheduler 451 / cli 35 |
| `duration_ms` | `float` | yes |
| `started_at` | `date` | yes (the time field — **no `@timestamp`** on these docs) |
| `orphans` (substrate, severity) | **`nested`** | **NO** in legacy Kibana viz (no nested bucket agg) |
| `substrate_checks` (substrate, status) | **`nested`** | **NO** in legacy Kibana viz |

> **`outcome` is the run-level orphan verdict** and reconciles with the nested data: nested `orphans.severity`
> = yellow 454 / red 140; top-level `outcome:red` = 140 (critical) and `outcome:yellow` = 42. So
> **`outcome:(red OR yellow)` == "orphans > 0"** — the ADR-0074 halt condition, available on a bare keyword
> with no nested agg required.
>
> Per-substrate orphan/check breakdown needs nested aggregation → **legacy Kibana viz cannot do it** →
> **DEFER** to a follow-up (Lens nested support, or a flattened per-substrate ES projection). Same
> measure-first/don't-fake move as FRE-536→547 and FRE-537→548.

---

## Panels — one dashboard `Monitors — Joinability & SLM Health`

Two self-contained index-patterns defined inline in the NDJSON (no edit to `data_views.ndjson`; mirrors how
`traversal_gate.ndjson` embeds its pattern; `_import?overwrite=true` dedupes):

- `agent-monitors-joinability-pattern` — title `agent-monitors-joinability-*`, `timeFieldName: started_at`
- `agent-monitors-slm-health-pattern` — title `agent-monitors-slm-health-*`, `timeFieldName: probed_at`

### Joinability (4 panels, index-pattern `agent-monitors-joinability-pattern`)

| # | Title | Type | Agg | Maps to ticket candidate |
|---|---|---|---|---|
| J1 | Joinability Outcome Over Time | stacked area | date_hist(`started_at`) × terms `outcome` | orphan count over time (red/yellow bands = halt visible) |
| J2 | Joinability Outcome Distribution | donut | count by `outcome` | join-success rate (green = success) |
| J3 | Runs With Orphans Detected | metric | count, query `outcome:(red OR yellow)` | **halt condition orphans>0 at a glance** |
| J4 | Joinability Probe Duration Over Time | line | date_hist(`started_at`) × avg `duration_ms` | probe operational health |

### SLM health (4 panels, index-pattern `agent-monitors-slm-health-pattern`)

| # | Title | Type | Agg | Maps to ticket candidate |
|---|---|---|---|---|
| S1 | SLM Reachability Over Time | area | date_hist(`probed_at`) × terms `reachable` | availability/reachability over time (false band = down) |
| S2 | SLM Availability Split | donut | count by `reachable` | reachability summary |
| S3 | SLM Probe Latency Over Time | line | date_hist(`probed_at`) × avg `probe_latency_ms` | latency to SLM |
| S4 | Recent Unreachable Probes | **saved search** | query `reachable:false`; cols `probed_at, status, error, probe_latency_ms, trace_id` | error-reason hints + status (straddle-safe via `_source`) |

Objects in file: **2 index-patterns + 7 visualizations + 1 search + 1 dashboard**. Grid: J1–J4 top two rows,
S1–S4 bottom two rows (w24×h15), mirroring `cost_budget`/`traversal_gate` layout.

---

## Steps

1. **TDD — failing test first.** `tests/scripts/test_monitors_dashboard.py` (mirrors `test_traversal_gate_dashboard.py`):
   - file exists + valid NDJSON (one JSON object/line);
   - exactly **one** `dashboard`; exactly **7** `visualization` + **1** `search`;
   - exactly the **two** expected index-patterns, each with the correct `id` **and** `timeFieldName`
     (`started_at` / `probed_at`);
   - every viz/search references one of the two monitor index-patterns (never `agent-logs-pattern`);
   - **straddle-trap guard:** the set of agg `params.field` across all viz ⊆
     `{outcome, source, started_at, duration_ms, reachable, probed_at, probe_latency_ms}` — i.e. **no agg on
     `status`, `status.keyword`, `error`, or any `.keyword` field** (the SLM straddle trap);
   - panel refs resolve; `panelsJSON` names == dashboard `references`;
   - registered in `import_dashboards.sh`.
   - Run → **fails** (file absent): `make test-file FILE=tests/scripts/test_monitors_dashboard.py` → red.
2. **Create `config/kibana/dashboards/monitors_joinability_slm.ndjson`** — copy `cost_budget`/`traversal_gate`
   visState shapes; metric viz for J3; saved-search shape from the `data_views.ndjson` `type:"search"` example
   for S4. Verify: step-1 test → **green**.
3. **Register in `config/kibana/import_dashboards.sh`** — append `"monitors_joinability_slm.ndjson"` to `FILES`.
4. **Local import + aggregation proof** (local Kibana :5601 / ES :9200 — *not* a deploy):
   `./config/kibana/import_dashboards.sh` → expect `OK monitors_joinability_slm.ndjson`; spot-run each panel's
   agg via `_search` and confirm non-empty buckets. Record output in the research doc.
5. **Research doc** `docs/research/2026-06-09-fre-538-monitors-dashboard.md` — straddle table, field-verification
   proof, panel table, import proof, defer note. Add to `docs/research/README.md` index.
6. **Follow-up ticket (Needs Approval, Telemetry Surface Audit):** "Per-substrate joinability breakdown panels
   — nested `orphans`/`substrate_checks` aggregation (requires Lens nested support or a flattened per-substrate
   ES projection)". File via Linear.
7. **Quality gates:** `make test-file` (new test) → `make test` (module) → `make mypy` → `make ruff-check` +
   `make ruff-format` → `pre-commit run --all-files`.
8. **PR** with `.github/PULL_REQUEST_TEMPLATE.md`, pre-merge checklist only. **STOP** (master merges/deploys/closes).

## Acceptance (FRE-538)

- [ ] Both monitor families have a populated dashboard (J1–J4 + S1–S4).
- [ ] Orphan halt condition (`outcome:(red OR yellow)` == orphans > 0) visible at a glance (J3 metric + J1 bands).
- [ ] Fields verified mapped/aggregatable before wiring; straddle avoided (`reachable`/`probe_latency_ms`, not
      `status`); test-enforced.
- [ ] Exported to version-controlled NDJSON, registered in import script, imports clean on local Kibana.
- [ ] Per-substrate nested breakdown deferred as a Needs-Approval follow-up (not faked).

## Out of scope / deferred (not faked)

- Per-substrate joinability breakdown (nested `orphans`/`substrate_checks`) — legacy aggs can't; follow-up.
- `status`/`error` term aggregations — straddled mapping; surfaced via saved-search `_source` instead.
- Rich SLM fields (gpu/vram/queue_depth/latency_ema) — null in current down-probe data; revisit when SLM is reachable.
