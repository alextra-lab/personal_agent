# Dashboard Triage — keep / retire / fix the 12 dashboards / 57 viz (FRE-535 / B1)

> **Date:** 2026-06-08 · **Ticket:** FRE-535 (B1, Tier-2:Sonnet) · **Project:** Telemetry Surface Audit
> **Executes off:** [FRE-533 (A1)](./2026-06-08-fre-533-telemetry-surface-reconciliation.md) reconciliation table (PR #193)
> **Spawned follow-up:** FRE-545 (wire `routing_decision` emission)
> **Reproduce:** `uv run python scripts/audit/verify_fre535_panels.py`

---

## TL;DR

Every visualisation in the repo Kibana saved objects was re-checked against **live
Elasticsearch data under the panel's own query/filter** (not just field existence) with
a purpose-built harness (`scripts/audit/verify_fre535_panels.py`). The harness is the
"no silent-empty panels" gate — red before the edits, green after.

The query-context-aware pass found **20 broken panels — broader than A1's static catalog
of 14** — because A1's dashboard corner read field references but not each panel's
`event_type`/`record_type` **filter**. Several panels have a perfectly-mapped bucket field
but filter on an `event_type` that **is never emitted**, so they are silently empty
regardless of the field.

| Disposition | Count | |
|---|---|---|
| **keep** (verified non-empty) | 44 panels | unchanged |
| **fix** — terms field rename | 9 panels | `.keyword`-on-bare-keyword + text-as-terms |
| **fix** — query key-drift / value | 3 panels | Extraction Retry Health (`event:`→`event_type:`) |
| **retire** — dead/never-emitted source | 12 panels | incl. the whole Request Latency dashboard (5) |

Result: **0 silent-empty panels** across the surviving surface (harness `PASS`).

Provenance gap (A1 finding #1) closed: the orphaned `prompt-cost-cache` dashboard is now
canonical and in the import script; the import script validates the `_import` **response
body** (not just HTTP 200); the index-pattern duplication is documented (full repoint
deferred — see D2).

---

## Method — measure, don't assert

Reproducing the FRE-433/434/533 methodology. The harness:

1. Parses every `config/kibana/dashboards/*.ndjson` saved object — classic `visState`
   aggregations **and** Lens `state` columns (Lens-aware, per codex review).
2. For each visualisation it extracts (a) the **bucket** fields (terms/segment aggs, not
   metric aggs), (b) the panel's `searchSourceJSON` **kuery filter**, and (c) the
   index-pattern it references.
3. Against live ES it checks: does the panel's filter match **≥1 doc**, and does every
   bucket field resolve to **≥1 aggregation bucket** under that filter? A `text`-typed
   field used as a terms agg errors (`fielddata disabled`) → caught as a failure.

Two refinements during the build (both from live measurement):

- **All-time, no `@timestamp` range.** An early `now-90d` lookback false-zeroed the
  Insights/Reflections panels — those families key on `timestamp`, not `@timestamp`.
  Querying ES directly, all-time is the most-permissive and correct gate.
- **Filter context is decisive.** It is what separates a `.keyword` mapping bug (fixable
  by field rename) from a dead `event_type` (retire/repoint).

---

## The full triage (12 dashboards / 57 viz)

Legend: ✅ keep · 🔧 fix · 🗑️ retire. Every 🔧 was re-verified non-empty live.

### LLM Performance — 6 fix, 2 keep
| Panel | Decision | Detail (live evidence) |
|---|---|---|
| LLM Call Count by Model | 🔧 | `model.keyword`→`model` — bare keyword, no `.keyword` subfield (5 buckets) |
| Avg Latency by Model Role | 🔧 | `role.keyword`→`role` (D1) — {primary, sub_agent} under `model_call_completed` |
| LLM Latency Over Time | 🔧 | `role.keyword`→`role` |
| P95 Latency by Role | 🔧 | `role.keyword`→`role` |
| Avg Prompt Tokens by Model Role | 🔧 | `role.keyword`→`role` |
| Prompt Token Percentiles by Role | 🔧 | `role.keyword`→`role` |
| LLM Errors Over Time | ✅ | 654 docs |
| Token Usage Over Time | ✅ | 9476 docs |

### System Health — 1 fix, 3 keep
| State Transitions | 🔧 | `from_state.keyword`→`from_state` (3 buckets) |
| CPU & Memory Timeline / Consolidation Events / Error Events | ✅ | non-empty |

### Insights Engine — 2 fix, 1 keep, 1 retire
| Insight count by type | 🔧 | `insight_type`→`insight_type.keyword` — `text` agg errored (`fielddata disabled`); 5 types live |
| Anomalies | 🔧 | `title`→`title.keyword` — **A1 missed this; harness caught the text-as-terms error** |
| Confidence trend | ✅ | 2577 docs |
| Weekly proposals created | 🗑️ | `proposals_created` 0 docs ever **and** filters `record_type:weekly_summary` (never emitted) |

### Extraction Retry Health — 3 fix
| Median attempts to success | 🔧 | filter `event:`→`event_type:consolidation_attempt_recorded` (the FRE-407/409 key-drift; 1827 docs) |
| Top denial_reason (donut) | 🔧 | same key-drift; `outcome:budget_denied` exists |
| Dead-letter rate (per role) | 🔧 | key-drift **+** `outcome:dead_letter`→`outcome:extraction_returned_fallback` (live outcomes: success / extraction_returned_fallback / budget_denied). *Panel now reflects the fallback outcome — title kept; semantics noted here.* |

### Delegation Outcomes — 1 keep, 3 retire
| Delegation volume by agent | ✅ | `target_agent`, 41 docs |
| Delegation success rate | 🗑️ | filters `delegation_outcome_recorded` = **0 ever** (delegation outcomes were never instrumented; only `delegation_package_created` exists) |
| Rounds needed trend | 🗑️ | `rounds_needed` 0 docs ever + same dead filter |
| Delegation satisfaction distribution | 🗑️ | `user_satisfaction` 0 docs ever + same dead filter |

### Task Analytics — 3 keep, 1 retire
| Entity Creation Rate / Memory Enrichment / Tasks Over Time | ✅ | non-empty |
| Routing Decisions | 🗑️ | filters `routing_decision` = **0 ever**; `target_model` never emitted. **Code-gap, not a relic** — `ROUTING_DECISION` is a defined-but-never-emitted constant. → **FRE-545** wires the emit; restore the panel after. |

### Request Timing (E2E) — 2 keep, 2 retire
| Request Count / Total Request Duration Over Time | ✅ | `request_timing`, 1444 docs |
| Avg Duration by Phase | 🗑️ | filters `request_timing_phase` = 0 ever; per-phase analysis lives in Request Traces |
| Request Phase Details | 🗑️ | same |

### Request Latency — **whole dashboard retired (5 panels)**
Every panel filters `request_latency_breakdown` / `request_latency_phase` = **0 docs ever**.
Fully superseded by **Request Traces** (`request_trace_step`, 16,020 docs — same phase /
duration / trace data, working). The `request_latency.ndjson` file is deleted and removed
from `import_dashboards.sh` + README.

### Request Traces — 4 keep
Phase Averages, Request Overview, Single Trace Waterfall, Trace Detail Table — all ✅
(`request_trace_step` / `request_trace`). This dashboard is the canonical per-phase /
per-trace surface.

### Expansion & Decomposition — 6 keep · Intent Classification — 4 keep · Reflection Insights — 4 keep
All verified non-empty (Reflections key on `timestamp`; ✅ once the harness time-field trap
was fixed). No edits.

### Prompt Cost & Cache Attribution (FRE-406) — 2 keep (newly canonical)
`Per-callsite token/cost breakdown`, `Static prefix hash stability` — Lens, on
`prompt_callsite`. Was orphaned in `docker/kibana/dashboards/` and **not** loaded live; now
canonical in `config/kibana/dashboards/` and in the import script.

---

## Provenance & round-trip (A1 finding #1 → B1)

- **Canonical path established.** All dashboards now live under `config/kibana/dashboards/`;
  the stray `docker/kibana/dashboards/prompt-cost-cache.ndjson` was `git mv`-d in and added
  to `import_dashboards.sh` + README. `docker/kibana/` is gone.
- **Import script hardened.** `import_dashboards.sh` now parses the `_import` **response
  body** (`success:true`, no `errors[]`) instead of trusting HTTP 200 — `_import` returns
  200 even when individual objects fail (codex review). Any per-object failure now exits 1.
- **D2 — index-pattern dedup: documented, not surgically repointed.** Two titles collide
  for the logs family: **`agent-logs*`** (id `eabfafeb-…`, embedded in `llm_performance` /
  `request_timing` / `request_traces`) and **`agent-logs-*`** (`agent-logs-pattern`, the
  canonical one, + a third id `prompt-cost-cache-data-view`). Captures (×2), reflections
  (×3), insights (×2) also ship duplicate copies. Full dedup means repointing every
  cross-file `references[]` entry to one surviving id — high-risk surgery deferred to a
  follow-up; **no `references[]` edits in this PR** (dangling-ref safety).

| Title | distinct ids | copies | note |
|---|---|---|---|
| `agent-logs-*` | `agent-logs-pattern`, `prompt-cost-cache-data-view` | 11 | canonical title |
| `agent-logs*` | `eabfafeb-…` | 4 | **redundant** — collapse into `agent-logs-*` (follow-up) |
| `agent-captains-captures-*` | 2 | 2 | dup |
| `agent-captains-reflections-*` | 2 | 3 | dup |
| `agent-insights-*` | 1 | 2 | dup copy |

- **Live ≠ repo (deferred to deploy).** The harness verifies repo NDJSON against live ES
  *data*; it does **not** require importing to Kibana. Re-importing the fixed dashboards and
  deleting the retired saved objects from live Kibana is a **post-merge deploy step**
  (master), not part of this build PR. See the post-merge note on the ticket.
- **Live-only viz (UI drift):** live Kibana carried 57 viz vs the repo's 54 — UI edits never
  exported (e.g. a case-dup `Request Count` / `Request count`, `Avg Input Tokens by Model
  Role`, `Input Token Percentiles by Role`). These are **not** adopted into the repo; the
  repo is the source of truth and the deploy step overwrites live from it.

---

## Decisions log

- **D1 (owner):** `role.keyword` panels + Routing repoint → **`role`** (not `model_role`).
  Under the panels' own `event_type:model_call_completed` filter both resolve to
  {primary, sub_agent}; `role` chosen for the richer breakdown in other contexts.
- **D2 (owner):** index-pattern dedup = **document-only** here; full repoint is a follow-up.
- **Routing Decisions (owner):** **retire** in B1; the missing `routing_decision` emit is a
  code gap tracked in **FRE-545** (links FRE-432 tier-routing gap, FRE-539/C4).
- **Request Timing phase panels (owner):** **retire** (covered by Request Traces).

## Acceptance (FRE-535)

- [x] Keep/retire/fix decision recorded for all 12 dashboards / 57 viz (this doc).
- [x] Every "fix" panel corrected and re-verified against live data — `verify_fre535_panels.py`
      `PASS` (0 silent-empty).
- [x] Retired panels removed; surviving dashboards version-controlled under
      `config/kibana/dashboards/` (canonical export path established).
- [x] Triage summary written to `docs/research/` (this file, dated).
