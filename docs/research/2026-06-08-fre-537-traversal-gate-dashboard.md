# FRE-537 (C2) — Traversal Ledger & Gate-Decision Dashboard

> **Date:** 2026-06-08 · **Ticket:** FRE-537 (Tier-2:Sonnet) · **Project:** Telemetry Surface Audit
> **Refs:** FRE-452 (route-trace ledger, Done) · FRE-506 (gate decision telemetry) · ADR-0088 + FRE-513 (topology spine) · A1 (FRE-533 reconciliation) · A2 (FRE-534 templates, Done) · C1 (FRE-536, the pattern this mirrors)
> **Plan:** `docs/superpowers/plans/2026-06-08-fre-537-traversal-gate-dashboard.md`

## TL;DR

The ticket asks for a dashboard over four L0-traversal families. Measuring against **live ES** before
designing (the FRE-536 discipline) split them cleanly into *ES-visible / build now* and *Postgres-or-transient
/ defer*. Two families have rich, aggregatable, **genuinely-unviewed** telemetry in `agent-logs-*`:
gate decisions (`tool_loop_gate`, 8 998 docs) and the route-trace ledger ES slice (`route_trace_written`).
The other two cannot be built honestly from ES: the **execution-topology label never reaches ES** (the
`turn.*` docs are the consumer's `event_processed` log line; `topology`/`cost_authoritative_usd` are absent
from `_field_caps`), and **decomposition strategy distribution is already viewed** on
`expansion_decomposition` + `intent_classification`. So C2 ships a 6-panel dashboard on the two real surfaces
and defers the topology view to a follow-up ES emitter — mirroring how C1 deferred cap-utilization to FRE-547.

## Where each candidate's data actually lives (measured, not assumed)

| Candidate (ticket) | ES reality | Verdict |
|---|---|---|
| Gate decisions (allow/deny, top reasons) | `tool_loop_gate` — 8 998 docs; `decision`/`reason`/`tool_name` bare `keyword`, aggregatable | **BUILD** (4 panels) |
| Route/trace ledger (stimulus→path→result) | `route_trace_written` — `gateway_label` + `orchestration_event` (ES slice). Rich per-turn fields (task_type/complexity/result_type/latency/cost) are **Postgres-only** (route-trace ledger table) | **BUILD ES slice** (2 panels) |
| Execution topology (primary vs sub-agent, `(trace_id, task_id)`) | `turn.topology_entered`/`turn.completed` ES docs = the consumer `event_processed` line; **`topology` + `cost_authoritative_usd` absent in `_field_caps`**. Label lives only in the Postgres ledger row + the transient AG-UI `turn_status` STATE_DELTA (ADR-0076 projector → UI, never persisted) | **DEFER** → follow-up |
| Decomposition / delegation path | `strategy` on `gateway_output` | **ALREADY VIEWED** — `expansion_decomposition` + `intent_classification`; not duplicated |

### Field verification (live `_field_caps`)

```
decision keyword | reason keyword | tool_name keyword | state_before/after keyword
gateway_label keyword | orchestration_event keyword | task_id keyword
topology  -> ABSENT     cost_authoritative_usd -> ABSENT   (confirms the defer)
```

Every dimension is **bare `keyword`** — panels reference `decision`, **not** `decision.keyword` (the A1 trap
that agg-resolves to nothing and silently empties the panel; 9 panels broke on it in A1). A static test
(`tests/scripts/test_traversal_gate_dashboard.py`) enforces the no-`.keyword`-suffix rule, that every panel
references the canonical `agent-logs-pattern`, and that every dashboard panel ref resolves.

## Panels (all on verified-typed fields, shared `agent-logs-pattern`)

| # | Panel | Source event | Agg | Maps to candidate |
|---|---|---|---|---|
| 1 | Gate Decisions Over Time | `tool_loop_gate` | area: date_hist × count, split `decision` | gate decisions (FRE-506) |
| 2 | Gate Decision Outcomes | `tool_loop_gate` | donut: count by `decision` | allow/deny split |
| 3 | Gate Activity by Tool | `tool_loop_gate` | table: `tool_name` × `decision` × count | gate by tool |
| 4 | Top Gate Block / Warn Reasons | `tool_loop_gate AND NOT decision:allow` | table: count by `reason` | top denial reasons |
| 5 | Route-Trace: Stimulus → Path Label | `route_trace_written` | donut: count by `gateway_label` | route ledger stimulus→path (FRE-452) |
| 6 | Route-Trace: Orchestration Outcome Over Time | `route_trace_written` | bar: date_hist × count, split `orchestration_event` | route ledger outcome |

Built as **legacy aggs-based** visualizations (not Lens) so `.kibana` `_import` accepts them strictly (the
Lens-based prompt-cost-cache dashboard fails strict import — FRE-546). File:
`config/kibana/dashboards/traversal_gate.ndjson`, registered in `import_dashboards.sh`.

## Live import + aggregation proof

`_import?overwrite=true` → `success=true, successCount=8` (index-pattern + 6 visualizations + dashboard, all
references resolve). The aggregations the panels run return real buckets:

```
decision (P1/P2):  allow 5367 · block_consecutive 1847 · block_identity 1035 · warn_consecutive 505 · advise_identity 237 · block_output 7
tool×decision (P3): bash 5133 -> allow 3163 / block_consecutive 1703 / block_identity 188 ; query_elasticsearch -> block_identity 255 / …
non-allow reason (P4): "Consecutive threshold reached (2/2)" 151 · "(3/3)" 92 · "terminal (10/10)" 84
gateway_label (P5): conversational/single 46 · tool_use/single 8 · analysis/single 6 · tool_use/hybrid 5
orchestration_event (P6): primary_handled 65 · delegate_called 7
```

The `gateway_label` (deterministic stimulus→path label) vs `orchestration_event` (primary_handled vs
delegate_called — what actually happened) pairing is the ES-visible expression of FRE-452's core "is the
label lying about the cognitive work" question.

## Deferred — execution-topology ES projection (follow-up, not faked)

The topology label (`primary`/`hybrid_fanout`/`decompose`/`delegate`), the per-`(trace_id, task_id)`
primary-vs-sub-agent rows, and the authoritative per-turn cost live only in the **Postgres route-trace
ledger** (durable sink, ADR-0088 D6) and the **transient AG-UI `turn_status`** (projector → UI, never
persisted). Kibana can read neither. Surfacing them needs a new periodic emitter that projects
`(trace_id, task_id, topology, role=primary|sub_agent, authoritative_cost_usd, result_type)` into an ES index
with explicit `keyword`/`double` mappings — the same shape as FRE-547's `budget_counter_snapshot`. Filed as a
Needs-Approval follow-up under Telemetry Surface Audit. The rich route-trace cognitive-work fields ride the
same emitter.

## Acceptance

- [x] Dashboard NDJSON in repo (`traversal_gate.ndjson`), registered in `import_dashboards.sh`.
- [x] Imports clean on local Kibana (`success=true`, 8 objects, no errors).
- [x] Panels populated against real data (per-panel agg proof above).
- [x] Every field verified `keyword`/aggregatable before wiring; no `.keyword`-on-bare-keyword refs (test-enforced).
- [x] Exported to version-controlled NDJSON.
- [x] Execution-topology + rich route-trace defer filed as a Needs-Approval follow-up (not faked).
