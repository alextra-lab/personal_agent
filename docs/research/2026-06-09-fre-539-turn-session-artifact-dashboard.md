# FRE-539 (C4) — Turn-level + E2E Session + Artifact-Envelope Dashboard

> **Date:** 2026-06-09 · **Ticket:** FRE-539 (Tier-2:Sonnet) · **Project:** Telemetry Surface Audit
> **Refs:** ADR-0089 (artifact envelope, D5 probe) · ADR-0081 (cross-turn KV reuse) · A1 (FRE-533 reconciliation) · A2 (FRE-534 templates) · B1 (FRE-535 triage) · C1/C2/C3 (FRE-536/537/538 — the dashboard pattern this mirrors)
> **Artifact:** `config/kibana/dashboards/turn_session_artifact.ndjson` · **Test:** `tests/scripts/test_turn_session_artifact_dashboard.py`
> **Plan:** `docs/superpowers/plans/2026-06-09-fre-539-turn-session-artifact-dashboard.md`

Closes the "new dev" list (C4): **one** dashboard — `C4 — Turn, Session & Artifact Analytics` — with three
labelled sections. Measure-first: every field was walked through live `_field_caps` + sample docs **before**
any panel was wired (the standing "you always get the mappings wrong first pass" rule), and every panel's
aggregation was re-run against live ES after import.

---

## Measure-first findings (live ES :9200, this session)

All three sections read **`agent-logs-*`** — every relevant event lands there, so the dashboard uses **one**
index-pattern. Join keys verified `keyword` + aggregatable (`_field_caps`): `trace_id`, `session_id`,
`task_id`, `artifact_id`, `user_id`, `slug` are **bare `keyword`** — the ticket's keyword constraint is met
with **no `.keyword`** suffix (a `.keyword` agg on a bare-keyword field is the A1 silent-empty trap;
test-guarded).

| Field | Type | Source event | Used by |
|---|---|---|---|
| `complexity` / `task_type` / `strategy` | keyword | `gateway_output` (~1/turn, 2272) | §1 |
| `cache_read_tokens` / `input_tokens` | long | `model_call_completed` | §1 |
| `cost_usd` (double), `latency_ms` (long), `session_id`, `trace_id` | — | `api_cost_recorded` (5417) | §2 |
| `probe_status` / `gate_decision` keyword; `envelope_ok` boolean; `http_status` long | — | `artifact_envelope_integrity` / `artifact_gate_decision` | §3 |

**Three constraints the data imposed (each changed the design):**

1. **`gateway_output` carries no `session_id`** (0/2272) — only `trace_id`. So turn classification
   (complexity/strategy) cannot roll up to session directly. "Turns per Session" (§2) is therefore
   `cardinality(trace_id)` over `api_cost_recorded` — **billable traces per session, a turns proxy, NOT a
   `gateway_output`→session join.** Named here so it is not mistaken for the gateway turn count.
2. **The artifact-envelope surface is near-empty — 5 docs total** (`artifact_gate_decision` 4 ·
   `artifact_envelope_integrity` 1). The single envelope doc is `probe_status:unverified_access_denied`
   (http 302); there are **zero `verified` probes and zero `envelope_ok` values**. The probe fires only on
   real edge commits, which barely happen on local infra. §3 is built **correctly-typed and join-ready** but
   is an **instrumentation-readiness surface**, not a validated one — an on-dashboard markdown banner makes
   the empty `Degraded Envelopes` panel read as "no verified probes yet," **not** as a healthy fleet.
3. **Session error-rate is not honestly supportable.** `level` is upper-case (`ERROR` 161/7d). ERROR events
   carry `trace_id` 119/161 but `session_id` only **5/161**, and 67 are synthetic `test_error_with_context`
   noise. So §2 surfaces **trace-level** error attribution (`terms(trace_id)`), not a session error-rate.

## Anti-duplication reconciliation (ticket: "extend, don't duplicate" the B1-kept dashboards)

| Candidate panel | Already shipped by | Decision |
|---|---|---|
| Decomposition **strategy** distribution | `expansion_decomposition` | **omit** (only a saved-search column) |
| **task_type** distribution | `intent_classification` | **omit** |
| Latency / tokens **by model/role** | `llm_performance` | **omit** |
| Cost over time / spend by role / **per-session cost** (`sum(cost_usd)` by `session_id`) | `cost_budget` | **omit** — §2 adds *non-cost* session dimensions |
| Single-trace waterfall | `request_traces` | **omit** — §2 adds the *aggregate* layer |
| **complexity** distribution / over time | nowhere | **NEW** (§1) |
| Cross-turn **`cache_read_tokens`** reuse | only `prompt-cost-cache` (broken on local Kibana, FRE-546) | **NEW** (§1) |
| **turns / calls per session** rollup | nowhere | **NEW** (§2) |
| Artifact envelope integrity / gate | nowhere | **NEW** (§3) |

## Index-pattern — shared, not inline (accepted trade)

Unlike FRE-538 (self-contained inline patterns), every panel references the **existing shared**
`agent-logs-pattern` (title `agent-logs-*`, tf `@timestamp`) from `data_views.ndjson` — used by
`request_timing` + `llm_performance`. This avoids adding a **4th** agent-logs index-pattern duplicate (FRE-533
flagged that duplication). **Accepted fragility:** a hard import-ordering dependency — `data_views.ndjson`
**must** import before this file (it is first in `import_dashboards.sh`), and a rename/delete/time-field change
of `agent-logs-pattern` breaks every panel. Recorded here and in the dashboard `description` so it is explicit.

---

## Panels (11 viz + 2 saved searches + 1 dashboard = 14 objects)

**§1 Turn-level** — `c4-turn-complexity-dist` (donut) · `c4-turn-complexity-time` (area) ·
`c4-turn-cache-reuse-time` (line, avg `cache_read_tokens` vs avg `input_tokens`) ·
`c4-turn-detail` (saved search: `trace_id,task_type,complexity,strategy,token_count,mode`).

**§2 Session / trace aggregate** — `c4-sessions-over-time` (line, cardinality `session_id`) ·
`c4-turns-per-session` (table, terms `session_id` → cardinality `trace_id`) ·
`c4-calls-per-session` (table, count + avg `latency_ms` by `session_id`) ·
`c4-errors-by-trace` (table, terms `trace_id` on `level:ERROR`).

**§3 Artifact-envelope (ADR-0089)** — `c4-artifact-note` (markdown readiness banner) ·
`c4-artifact-probe-status` (donut, `probe_status`) · `c4-artifact-degraded` (metric, `envelope_ok:false` alarm) ·
`c4-artifact-gate-time` (area, `gate_decision`) ·
`c4-artifact-detail` (saved search joinable on `artifact_id`).

## Import + per-panel aggregation proof (local Kibana :5601 / ES :9200 — NOT a deploy)

```
./config/kibana/import_dashboards.sh
  OK    turn_session_artifact.ndjson

§1 complexity (gateway_output):     simple 2055 / moderate 191 / complex 26
§1 cross-turn KV reuse:             avg cache_read 13760.5 vs avg input 11825.5  (read > input ⇒ strong reuse)
§2 distinct sessions:               152
§2 turns/session (top):             40 / 26 / 45 distinct traces
§2 calls/session (top):             58 calls @7059ms · 52 @15913ms · 50 @2189ms
§2 errors/trace (top):              test-tra* 286 (synthetic noise) · 2ebf1f03 102 · 610a486e 55
§3 probe_status:                    unverified_access_denied 1   (0 verified — see banner)
§3 degraded (envelope_ok:false):    0                            (0 verified probes, not "healthy")
§3 gate_decision:                   committed 4
```

> **Note (pre-existing, not introduced here):** `prompt-cost-cache.ndjson` still fails to import on local
> Kibana (`strict_dynamic_mapping_exception` on its Lens objects) — unchanged by this ticket (zero diff),
> already tracked as **FRE-546**. Out of scope for C4.

## Acceptance (FRE-539) — met

- ✅ **Turn-level + session-aggregate + artifact-envelope panels live, populated** — §1/§2 populate from real
  data (proof above); §3 is built correctly-typed + join-ready and populates as artifacts serve (5 docs today),
  framed as instrumentation-readiness, **not faked**.
- ✅ **Join keys verified `keyword`; no overlap/duplication with kept B1 dashboards (documented)** —
  measure-first table + anti-duplication table above; the test pins bare-keyword join fields and forbids
  `.keyword`.
- ✅ **Exported to version-controlled NDJSON** — `config/kibana/dashboards/turn_session_artifact.ndjson`,
  registered in `import_dashboards.sh`, imports clean.

## Deferred (not faked) — Needs-Approval follow-ups

- **Artifact-envelope panels are data-starved** (5 docs, 0 verified, 0 `envelope_ok`). The `c4-artifact-degraded`
  alarm is wired but **unproven against a real degraded envelope** — revisit once edge commits populate the
  surface.
- **Session error-rate** — `level:ERROR` lacks `session_id` (5/161) and is test-noisy; surfaced as a trace-level
  error table instead. A true session error-rate needs error events to thread `session_id`.
- **Per-session token totals** — `api_cost_recorded` has no token fields; a token rollup needs a
  `model_call_completed` × `session_id` join, deferred to keep §2 single-source.
