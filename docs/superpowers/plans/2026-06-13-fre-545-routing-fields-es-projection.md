# FRE-545 — Restore Routing Decisions observability (project ledger routing fields to ES)

**Ticket:** FRE-545 (Approved, Tier-2:Sonnet) · **Project:** Telemetry Surface Audit
**Refs:** ADR-0088 (route-trace ledger) · FRE-452 (ledger) · FRE-517 (per-topology rows) · **FRE-548 (the `agent-topology-*` projection emitter this extends)** · FRE-535 (retired the dead panel) · FRE-539 (C4 — panel rebuild home) · ADR-0074 (identity)

## Re-anchored framing (master, 2026-06-10)

Do **not** wire the dead `routing_decision` event or invent a new taxonomy. The authoritative routing record is the ADR-0088 route-trace ledger; FRE-545 projects its routing fields to ES using the **same FRE-548 emitter** (don't fork a second emit path).

## Empirical findings (verified in code)

- `heuristic_routing()` / `HeuristicRoutingPlan` (`orchestrator/routing.py`) are **never called** in the live path; `ctx.routing_history` is **never populated** (no writes in `executor.py`). So the routing-*plan* fields the ticket names — `target_model`, `reason`, `used_heuristics` — **do not exist** in the live ledger.
- `RouteTraceRow.thinking_enabled` is **hardcoded `None`** by the FRE-517 assembler — always absent today (capturing it is out of scope; owner-confirmed).
- The ledger row **does** carry, populated by `assemble_route_trace`: `model_role` (selected tier — always `primary` today, *which is the FRE-432 tier-routing gap this surface makes measurable*), `intent_confidence`, `decomposition_strategy`, `decomposition_reason`.
- FRE-548's `build_topology_doc` already projects `task_type`, `complexity`, `role`, `gateway_label`, `result_type` to `agent-topology-*`.
- The 4 `ROUTING_*` constants (`telemetry/events.py:110-114`) are **unreferenced anywhere** (grep) → safe to delete.

## Decisions (owner-confirmed 2026-06-13)

1. **Project the 4 populated fields** (skip the dead/absent `target_model` / `used_heuristics` / `thinking_enabled`):

| ES field | Type | Source (RouteTraceRow) | Notes |
|---|---|---|---|
| `model_role` | keyword | `model_role` (str\|None) | the tier — ticket's `target_model/model_role`; `sub_agent` on segment rows, `primary` on turn-level (today always primary = the gap) |
| `intent_confidence` | float | `intent_confidence` (float\|None) | ticket's `confidence`; emit `float(...)`, omit when None |
| `decomposition_strategy` | keyword | `decomposition_strategy` (str\|None) | the gateway routing decision; omit when None |
| `decomposition_reason` | keyword | `decomposition_reason` (str\|None) | ticket's `reason` (short machine string); omit when None |

   Segments carry only `model_role` (the others are gateway-turn fields → None → omitted), matching the omit-None discipline.

2. **Defer the Routing Decisions panel to FRE-539/C4** (mirror FRE-548→FRE-537). Leave a schema-contract comment on FRE-539.

## Files

### 1. `src/personal_agent/observability/topology/es_projection.py` — extend `build_topology_doc`
Add, after the existing omit-None block (all `keyword`/`float`, omitted when None):
```python
if row.model_role is not None:
    doc["model_role"] = row.model_role
if row.intent_confidence is not None:
    doc["intent_confidence"] = float(row.intent_confidence)
if row.decomposition_strategy is not None:
    doc["decomposition_strategy"] = row.decomposition_strategy
if row.decomposition_reason is not None:
    doc["decomposition_reason"] = row.decomposition_reason
```
Update the docstring's omit-None list (codex note: keep it accurate — it describes the
*path-dependent* fields that are omitted when None; do **not** claim "all None fields omitted",
since the pre-existing `session_id` is still emitted as `null` when absent — unchanged FRE-548
behavior, and an ADR-0074 identity field that is effectively always present anyway). **No change to
`project_route_trace_to_es` or the seam** — the emitter + both call sites are already wired (FRE-548).

### 2. `docker/elasticsearch/topology-index-template.json` — explicit mappings (REQUIRED)
The template is `dynamic: false`, so a field absent from `properties` is **stored-but-not-indexed** (un-queryable/un-aggregatable). Add to `properties`:
```json
"model_role":             { "type": "keyword" },
"intent_confidence":      { "type": "float" },
"decomposition_strategy": { "type": "keyword" },
"decomposition_reason":   { "type": "keyword", "ignore_above": 1024 }
```
`decomposition_reason` gets `ignore_above: 1024` (codex note): its source values are short machine
strings today (`memory_recall_always_single`, `tool_use_complex_hybrid`), but the DTO docstring
calls it "human-readable rationale" — `ignore_above` means a future long reason is stored-not-
indexed rather than **rejecting the whole doc** at the Lucene 32 KB term limit. The other keyword
fields are short enums and need no cap. Update `_meta.description`.

### 3. `src/personal_agent/telemetry/events.py` — delete the 4 dead constants
Remove the `# Routing events (Day 11.5)` block (`ROUTING_DECISION` / `ROUTING_DELEGATION` / `ROUTING_HANDLED` / `ROUTING_PARSE_ERROR`). Grep-confirmed unreferenced; the ledger is the routing surface.

*Adjacent dead code NOT touched (out of named scope; mention only):* `heuristic_routing()` + `HeuristicRoutingPlan` (`orchestrator/routing.py`, `orchestrator/types.py`) are also never called — left in place; a separate cleanup ticket can remove them.

### 4. `docs/skills/seshat-observations.md` — extend the `agent-topology-*` field list
Add the 4 routing fields to the schema paragraph I added in FRE-548.

## Tests (TDD — extend the FRE-548 suite, write first / confirm red)

### `tests/observability/topology/test_es_projection.py`
- `test_turn_level_doc_includes_routing_fields`: a turn-level row with `model_role="primary"`, `intent_confidence=0.82`, `decomposition_strategy="single"`, `decomposition_reason="memory_recall_always_single"` →  doc has all 4, `intent_confidence` is a `float`.
- `test_segment_doc_omits_gateway_routing_fields`: a segment row (`model_role="sub_agent"`, the gateway fields None) → doc has `model_role="sub_agent"`, and **no** `intent_confidence` / `decomposition_strategy` / `decomposition_reason`.
- Extend `_turn_level_row` / `_segment_row` helpers with the new fields.

### Template validity
`python3 -c "import json; json.load(open('docker/elasticsearch/topology-index-template.json'))"` and confirm the 4 new keys + types.

### No-regression
`grep -rn "ROUTING_DECISION\|ROUTING_DELEGATION\|ROUTING_HANDLED\|ROUTING_PARSE_ERROR" src/ tests/` → empty after deletion (no broken imports).

## Quality gates
`make test-file FILE=tests/observability/topology/test_es_projection.py` → `make test` → `make mypy` → `make ruff-check`/`format` → `pre-commit run --all-files`.

## Post-deploy (Linear comment for master — NOT in PR checklist)
- Re-register the template (`scripts/setup-elasticsearch.sh`) so the 4 new explicit mappings apply before the next `agent-topology-*` daily index; additive `PUT agent-topology-<today>/_mapping` for the 4 fields if today's index already exists.
- `_field_caps` proof: `model_role`/`decomposition_*` → `keyword`, `intent_confidence` → `float`.

## Follow-up / coordination
- Comment on **FRE-539** (C4): the routing surface is live on `agent-topology-*`; schema contract for the Routing Decisions panel.

## Halt-condition check
Single coherent extension of the FRE-548 emitter (4 fields + template + dead-constant delete). No historical rows dropped. No ADR-phase bundling. No expected mypy regression. One phase = one PR.
