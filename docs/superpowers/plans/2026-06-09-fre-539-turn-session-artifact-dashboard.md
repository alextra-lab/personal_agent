# FRE-539 (C4) — Turn-level + E2E Session + Artifact-Envelope Dashboard

> **Date:** 2026-06-09 · **Ticket:** FRE-539 (Tier-2:Sonnet) · **Project:** Telemetry Surface Audit
> **Refs:** ADR-0089 (artifact envelope, D5 probe) · ADR-0081 (cross-turn KV reuse) · A1 (FRE-533 reconciliation) · A2 (FRE-534 templates) · B1 (FRE-535 triage / kept dashboards) · C1/C2/C3 (FRE-536/537/538 — the dashboard pattern this mirrors)
> **Artifact:** `config/kibana/dashboards/turn_session_artifact.ndjson` · **Test:** `tests/scripts/test_turn_session_artifact_dashboard.py`

Closes the "new dev" list: turn-level analytics, session-aggregate E2E joins, artifact-envelope
integrity — **one** dashboard, three labelled sections (the ticket's "one dashboard or small set").

---

## Measure-first findings (live ES :9200, this session — drive every design choice)

All three views read **`agent-logs-*`** (every relevant event lands there). Join keys verified `keyword`
(A2's work — `_field_caps`): `trace_id`, `session_id`, `task_id`, `artifact_id`, `user_id`, `slug` all
**bare `keyword`, aggregatable**. The ticket's keyword constraint is satisfied with **no `.keyword`** suffix
(adding one is the A1 silent-empty trap — test-guarded below).

**Field types (verified, aggregatable):** `complexity` keyword · `task_type` keyword · `strategy` keyword ·
`cost_usd` double · `cache_read_tokens` long · `input/output/total_tokens` long · `latency_ms` long ·
`envelope_ok`/`csp_present`/`mime_ok`/`nosniff_ok` boolean · `gate_decision`/`probe_status`/`served_mime`/`commit_path`
keyword · `http_status`/`probe_duration_ms` long.

**Per-turn classification source:** `gateway_output` (~1/turn, 2272 docs) carries `complexity`, `task_type`,
`strategy`, `token_count`, `mode`, `trace_id`. ⚠️ **`gateway_output` carries NO `session_id`** (0/2272) — so
turn-classification rolls up only by `trace_id`, never directly to session.

**Per-call cost/latency source:** `api_cost_recorded` (5417 docs) carries `cost_usd`, `latency_ms`, **both**
`session_id` + `trace_id`, `model`. Last 7d: **79 sessions / 150 traces / $20.16** — real, joinable.

**Cross-turn KV reuse source:** `model_call_completed` carries `cache_read_tokens` + `input_tokens` (the
ADR-0081 reuse signal). `prompt-cost-cache.ndjson` (FRE-406) is the only prior cache surface and it is
**broken** on local Kibana (Lens import failure, tracked **FRE-546**) — so this is the live cache panel.

**Artifact-envelope surface is near-empty — 5 docs total** (`artifact_gate_decision` 4 · `artifact_envelope_integrity` 1).
The single envelope doc is `probe_status:unverified_access_denied` (http 302) — **zero `verified` probes, zero
`envelope_ok` values yet**. The probe only fires on real edge commits, which barely happen on local infra.
Panels are built **correctly-typed and join-ready** but will populate only as artifacts are served — documented
as awaiting-data, **not faked** (same posture as C3's deferred nested breakdown).

**Errors:** `level` is upper-case (`ERROR` 161/7d, not `error`). ERROR events carry `trace_id` 119/161 but
`session_id` only **5/161**, and 67 are synthetic `test_error_with_context` noise → **session error-rate is
not honestly supportable**; trace-level error attribution is. Surfaced as a trace-keyed error table, **not** a
session error-rate metric.

## Anti-duplication reconciliation (against B1-kept dashboards — ticket constraint)

| Candidate panel | Already covered by | Decision |
|---|---|---|
| Decomposition **strategy** distribution | `expansion_decomposition` | **omit** (reuse via saved-search column only) |
| **task_type** distribution | `intent_classification` | **omit** |
| Latency / tokens by model/role | `llm_performance` | **omit** |
| Cost over time / spend by role / **per-session cost** (`sum(cost_usd)` by `session_id`) | `cost_budget` | **omit** — session view adds *non-cost* dimensions |
| Single-trace waterfall | `request_traces` | **omit** — session view adds the *aggregate* layer |
| **complexity** distribution / over time | nowhere | **NEW** |
| Cross-turn **cache_read_tokens** reuse | only broken `prompt-cost-cache` (FRE-546) | **NEW** |
| **Turns/calls per session** rollup | nowhere | **NEW** |
| Artifact envelope integrity / gate | nowhere | **NEW** |

---

## Index-pattern strategy

Reference the **existing canonical** `agent-logs-pattern` (title `agent-logs-*`, tf `@timestamp`) from
`data_views.ndjson` — used by `request_timing` + `llm_performance`. `data_views.ndjson` loads first in
`import_dashboards.sh`, so the reference resolves. **No new index-pattern object** (FRE-533 flagged
agent-logs index-pattern duplication; do not add a 4th). Every panel references `agent-logs-pattern`.

**Accepted fragility (codex Q3):** unlike FRE-538's self-contained inline patterns, this dashboard has a hard
**import-ordering dependency** — `data_views.ndjson` MUST import before this file, and a rename/delete/time-field
change of `agent-logs-pattern` breaks every panel. This trade (dedupe over self-containment) matches FRE-537 and
the A1 dedupe lesson; it is recorded in the NDJSON header comment **and** the research doc so it is not implicit.

## Panels — one dashboard `C4 — Turn, Session & Artifact Analytics` (id `turn-session-artifact-dashboard`)

Each viz carries its own event-type filter in `searchSourceJSON` (`kibanaSavedObjectMeta`) so one
index-pattern serves all three sections.

**Section 1 — Turn-level (filter `event_type:gateway_output` / `model_call_completed`):**

| id | Title | Type | Agg | Filter |
|---|---|---|---|---|
| `c4-turn-complexity-dist` | Turn Complexity Distribution | donut | terms(`complexity`) | `event_type:gateway_output` |
| `c4-turn-complexity-time` | Turn Complexity Over Time | stacked area | date_histogram(`@timestamp`) × terms(`complexity`) | `event_type:gateway_output` |
| `c4-turn-cache-reuse-time` | Cross-Turn KV Cache Reuse Over Time | line | avg(`cache_read_tokens`) + avg(`input_tokens`) × date_histogram | `event_type:model_call_completed` |
| `c4-turn-detail` | Turn Classification Detail | saved search | cols `trace_id,task_type,complexity,strategy,token_count,mode` | `event_type:gateway_output` |

**Section 2 — Session / trace E2E aggregate (filter `event_type:api_cost_recorded`):**

| id | Title | Type | Agg | Filter |
|---|---|---|---|---|
| `c4-sessions-over-time` | Active Sessions Over Time | line | cardinality(`session_id`) × date_histogram | `event_type:api_cost_recorded` |
| `c4-turns-per-session` | Turns per Session (top 20) | table | terms(`session_id`,20) → cardinality(`trace_id`) | `event_type:api_cost_recorded` |

> **codex Q2 caveat (must be stated in the research doc):** "Turns per Session" counts `cardinality(trace_id)`
> over `api_cost_recorded` docs — i.e. traces that produced a cost record, **not** a true
> `gateway_output`→session join. That join is impossible (gateway_output has no `session_id`), so the metric is
> "billable traces per session," a close proxy for turns, not the gateway turn count. Named, not hidden.
| `c4-calls-per-session` | LLM Calls & Avg Latency per Session (top 20) | table | terms(`session_id`,20) → count + avg(`latency_ms`) | `event_type:api_cost_recorded` |
| `c4-errors-by-trace` | Top Traces by Error Events | table | terms(`trace_id`,20) → count | `level:ERROR` |

**Section 3 — Artifact-envelope integrity (ADR-0089; filter on the two artifact events):**

| id | Title | Type | Agg | Filter |
|---|---|---|---|---|
| `c4-artifact-note` | (section banner) | **markdown** | — instrumentation-readiness framing (see below) | none |
| `c4-artifact-probe-status` | Envelope Probe Status | donut | terms(`probe_status`) | `event_type:artifact_envelope_integrity` |
| `c4-artifact-degraded` | Degraded Envelopes (alarm) | metric | count | `event_type:artifact_envelope_integrity and envelope_ok:false` |
| `c4-artifact-gate-time` | Gate Decisions Over Time | stacked area | date_histogram × terms(`gate_decision`) | `event_type:artifact_gate_decision` |
| `c4-artifact-detail` | Artifact Envelope Detail (join on artifact_id) | saved search | cols `artifact_id,trace_id,session_id,probe_status,envelope_ok,served_mime,http_status,gate_decision` | `event_type:(artifact_envelope_integrity or artifact_gate_decision)` |

**`c4-artifact-note` markdown (codex Q1/Q4 — make the empty surface unambiguous):** a `markdown`-type
visualization (no index-pattern, no aggs) stating: *"ADR-0089 D5 instrumentation-readiness surface. As of
2026-06-09 the served-envelope probe has emitted 5 docs / 0 verified probes — the probe fires only on real
edge commits. A zero on **Degraded Envelopes** means **no verified probes yet**, NOT a healthy fleet. Panels
are correctly typed and join-ready; they validate as artifacts are served."* So the zero-degraded reading
can't be mistaken for green.

→ 11 visualizations (incl. 1 markdown banner) + 2 saved searches + 1 dashboard = 14 objects. Inline-built JSON
mirroring the FRE-538 NDJSON shape (legacy aggs `visualization`, `search` with `columns`/`sort`, `dashboard`
with `panelsJSON`+`references`). The markdown viz carries no aggs and no index-pattern reference — the test's
agg-field / index-pattern guards iterate only panels that have aggs.

---

## Steps

1. **Write the static test first** `tests/scripts/test_turn_session_artifact_dashboard.py` (mirror
   `test_monitors_dashboard.py`): asserts (a) NDJSON valid, 1 dashboard + 11 viz (incl. 1 markdown) + 2 searches;
   (b) **every viz/search that has an index-pattern reference** points at `agent-logs-pattern` and nothing else
   (the markdown banner has **no** index-pattern ref — exempt by construction: iterate only panels with aggs/refs);
   (c) **no agg field ends in `.keyword`** (A1 trap); (d) every agg field ∈ verified-safe set
   `{complexity,task_type,strategy,session_id,trace_id,cache_read_tokens,input_tokens,latency_ms,probe_status,
   gate_decision,envelope_ok,@timestamp}`; (e) dashboard panel refs resolve (all 13 objects); (f) registered in
   `import_dashboards.sh`. → **run, confirm it fails** (file absent).
2. **Build** `config/kibana/dashboards/turn_session_artifact.ndjson` — 12 objects, hand-authored from the
   table above; every panel `searchSourceJSON` filter set per the Filter column.
3. **Register** in `config/kibana/import_dashboards.sh` `FILES=( … )` (append after `monitors_joinability_slm.ndjson`).
4. **Make test pass** → `make test-file FILE=tests/scripts/test_turn_session_artifact_dashboard.py`.
5. **Live import + per-panel agg proof** (local Kibana :5601 — NOT a deploy):
   `./config/kibana/import_dashboards.sh` → expect `OK turn_session_artifact.ndjson`; then curl each agg
   against ES :9200 and record bucket counts in the research doc (complexity buckets populate; turns-per-session
   cardinality > 0; artifact panels return the 5 sparse docs / document empties).
6. **Research doc** `docs/research/2026-06-09-fre-539-turn-session-artifact-dashboard.md` — measure-first
   findings, anti-duplication table, per-panel agg proof, deferred list.
7. **Quality gates:** `make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` ·
   `pre-commit run --all-files`. (No `src/` change → mypy/ruff are no-ops but run for parity; test file is pure stdlib.)
8. **PR** with template, pre-merge checklist only. STOP.

## Acceptance mapping (FRE-539)

- **Turn + session-aggregate + artifact panels live, populated** → §1/§2 populate from real data; §3 built
  correctly-typed, populates as artifacts serve (5 docs today) — documented awaiting-data, not faked.
- **Join keys verified `keyword`; no overlap with kept B1 dashboards (documented)** → measure-first + anti-dup
  table above; test pins bare-keyword join fields.
- **Exported to NDJSON in repo** → `config/kibana/dashboards/turn_session_artifact.ndjson` + import-script reg.

## Deferred (not faked) — file as Needs-Approval follow-ups

- **Artifact-envelope panels are data-starved** (5 docs, 0 verified) — revisit once edge commits populate
  `envelope_ok`; the alarm panel (`c4-artifact-degraded`) is wired but unproven against a real degraded envelope.
- **Session error-rate** — `level:ERROR` lacks `session_id` (5/161) and is test-noisy; surfaced as trace-level
  error table instead. A proper session error-rate needs error events to thread `session_id`.
- **Per-session token totals** — `api_cost_recorded` has no token fields; token rollup would need a
  `model_call_completed` × `session_id` join (model_call_completed has session_id) — deferred to keep §2 single-source.
