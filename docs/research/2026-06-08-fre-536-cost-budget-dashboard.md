# FRE-536 (C1) — Cost & Budget Dashboard (cost_gate / ADR-0065)

> **Date:** 2026-06-08 · **Ticket:** FRE-536 (Tier-2:Sonnet) · **Project:** Telemetry Surface Audit
> **Refs:** ADR-0065 (cost gate) · A1 (FRE-533 reconciliation) · A2 (FRE-534 templates) · B1 (FRE-535 triage) · FRE-546 (prompt-cost-cache import bug)
> **Plan:** `docs/superpowers/plans/2026-06-08-fre-536-cost-budget-dashboard.md`

## TL;DR

The cost & budget dashboard the ticket sketches could not be built as-was: `cost_gate/gate.py`
emitted its money fields as `str(Decimal)`, so Elasticsearch mapped them as `keyword` and they could
not be summed. Per the owner's call ("fix emit first, then full dashboard") we corrected the emit
sites + ES mappings, verified the fix against live ES, then built the dashboard on fields confirmed
numerically aggregatable. One panel — **cap utilization vs configured caps** — is deferred: that state
lives only in Postgres `budget_counters`, invisible to Kibana, and needs a new snapshot emitter
(follow-up ticket).

## Root cause — `str(Decimal)` → keyword

`telemetry/es_handler.py` builds the ES document by testing each value with `json.dumps(value)` and,
on `TypeError`/`ValueError`, falling back to `str(value)` (es_handler.py:158-165). A `Decimal` is not
JSON-serializable, so it would stringify regardless — and `gate.py` made it explicit with `str(amount)`.
A1 (FRE-533) classified all four — `amount`, `actual_cost`, `reserved`, `delta` — as
`keyword (text+keyword default)`, ⚠️ emitted-but-unmapped. FRE-534 (A2) corrected templates but did
**not** touch these emit sites.

## The fix

**Emit (numbers, namespaced):** emit `float(...)` under self-describing `*_usd` names. Renaming (not
reusing the generic names) avoids a same-name `keyword`→`double` conflict in existing indices, so **no
destructive reindex** is needed; historical string values are simply orphaned (they were never
aggregatable). `role` was added to `cost_gate_committed`/`cost_gate_refunded` so settled spend can be
attributed by budget role.

| Event | Old key (str→keyword) | New key (float→double) |
|---|---|---|
| `cost_gate_reserved` / `cost_gate_reserve_uncapped` | `amount` | `amount_usd` |
| `cost_gate_committed` | `actual_cost` / `reserved` / `delta` | `actual_cost_usd` / `reserved_usd` / `delta_usd` |
| `cost_gate_refunded` | `amount` | `amount_usd` |
| `litellm_request_budget_denied` + `model_call_started` | `reservation_amount` | `reservation_amount_usd` |

**Mapping:** added explicit `{"type":"double"}` for the five `*_usd` fields to
`docker/elasticsearch/index-template.json` (governs new indices). `scripts/setup-elasticsearch.sh`
reads the template file directly, so no script change was needed.

## Live verification (`_field_caps`)

Templates apply only to newly-created indices, so today's `agent-logs-2026.06.08` was patched
additively (the `*_usd` names are new → `PUT _mapping` adds them as `double` with no conflict). After
registering the updated `agent-logs-template` and the additive mapping:

```
$ curl 'localhost:9200/agent-logs-*/_field_caps?fields=amount_usd,actual_cost_usd,reserved_usd,delta_usd,reservation_amount_usd,cost_usd,role,event_type,budget_role,session_id'
actual_cost_usd:        double
amount_usd:             double
reserved_usd:           double
delta_usd:              double
reservation_amount_usd: double
cost_usd:               double   (pre-existing explicit)
role:                   keyword
event_type:             keyword
budget_role:            keyword
```

Every money field resolves to `double` (aggregatable); every dimension is bare `keyword` (use `role`,
**not** `role.keyword` — the A1 trap that broke six LLM-Performance panels).

## Panels (all on verified-typed fields)

| # | Panel | Source event | Field(s) | Agg |
|---|---|---|---|---|
| 1 | Actual Spend Over Time | `cost_gate_committed` | `actual_cost_usd` (double) | date_hist × sum |
| 2 | Spend by Budget Role | `cost_gate_committed` | `actual_cost_usd` × `role` (kw) | donut: sum by terms |
| 3 | Reserve/Commit/Refund Funnel | `cost_gate_*` | `event_type` (kw) | bar: count by terms |
| 4 | Net Settlement Delta | `cost_gate_committed` | `delta_usd` (double) | line: date_hist × sum |
| 5 | Budget Denials | `litellm_request_budget_denied` | `budget_role` (kw) | line: count × terms |
| 6 | Top Sessions by Model Spend | `model_call_completed` | `cost_usd` × `session_id` (kw) | table: sum by terms |

Built as legacy aggs-based visualizations (not Lens) so `_import` accepts them — the Lens-based
prompt-cost-cache dashboard fails strict `.kibana` import (FRE-546). All panels reference the canonical
shared index-pattern id `agent-logs-pattern` (no duplicate-index-pattern proliferation — A1 dedupe
lesson). File: `config/kibana/dashboards/cost_budget.ndjson`, registered in `import_dashboards.sh`.

**Import + aggregation proof.** `_import?overwrite=true` → `success=true, successCount=8, errors=none`
(all 8 objects + references resolve). Sample docs indexed; the aggregations the panels run return values:

```
sum(actual_cost_usd) where event_type=cost_gate_committed  -> 0.4   (numeric sum on a field that was str→keyword)
terms(role) on committed                                   -> [fre536_probe]
terms(session_id) × sum(cost_usd) on model_call_completed  -> real sessions, e.g. 2.13, 1.64, 0.33 USD
```

Panel 6 (Top Sessions) populates from pre-existing real `model_call_completed` data immediately; the
cost_gate panels populate as reserve/commit/refund/denial events accrue post-deploy.

## Reindex / rollover note

- New `*_usd` field names → **no destructive reindex**. The corrected `double` mapping is picked up by:
  (a) every new daily `agent-logs-YYYY.MM.DD` index via the updated template, and (b) today's index via
  the additive `PUT _mapping`.
- Historical `amount`/`actual_cost`/`reserved`/`delta` `keyword` values remain in old indices, orphaned
  and unused by the dashboard. They were never numerically usable, so nothing of value is lost. Dollar
  panels populate from the fix forward.

## Deferred — cap utilization (follow-up)

"Cap utilization vs configured caps" needs `running_total` / `cap_usd` from Postgres `budget_counters`,
which Kibana cannot read. There is no threshold-hit ES event (a `BudgetDenied` is raised before any
emit in the reserve path). This requires a new periodic emitter logging a `budget_counter_snapshot`
event (role, time_window, running_total, cap_usd, utilization_ratio) with explicit `double` mappings —
filed as **FRE-547** (Needs Approval, Telemetry Surface Audit).

## Acceptance

- [x] Emit fix: money fields land as `double` (live `_field_caps` proof above).
- [x] Exported to version-controlled NDJSON + registered in import script.
- [x] Every field verified mapped at correct type before wiring (no silent-empty / wrong-agg panels).
- [x] Reindex/rollover note recorded.
- [x] Dashboard imported + panels populated on local Kibana (import success=true/8 objects; aggregations return values).
- [x] Cap-utilization follow-up filed — **FRE-547** (Needs Approval).
