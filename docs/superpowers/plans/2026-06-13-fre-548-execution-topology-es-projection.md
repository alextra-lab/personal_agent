# FRE-548 — Execution-topology ES projection emitter

**Ticket:** FRE-548 (Approved, Tier-2:Sonnet) · **Project:** Telemetry Surface Audit
**Refs:** ADR-0088 (topology spine) · FRE-452 (ledger) · FRE-513 (seam) · FRE-517 (per-segment rows — this PR's source) · FRE-547 (sibling Postgres→ES snapshot-emitter pattern) · FRE-537 (C2 dashboard this unblocks)

## Problem

The topology label, the per-`(trace_id, task_id)` primary-vs-sub-agent rows, the authoritative per-turn cost, and the route-trace cognitive-work fields live **only** in the Postgres route-trace ledger and the transient AG-UI `turn_status`. Kibana can read neither, so FRE-537 couldn't build the execution-topology panel.

## Approach (owner-confirmed: **on-write at the seam**)

When `observe_topology` writes each `RouteTraceRow` to Postgres (turn-level via `_write_durable_row`, segments via `_write_segment_rows`), **also project that same in-hand row** to a dedicated `agent-topology-*` ES index — a third best-effort sink at the existing ADR-0088 seam. Reuses the lifespan-wired `get_es_indexer()` (via the proven non-blocking `schedule_es_index`), idempotent on `doc_id = trace_id:task_id` (mirrors the Postgres key + the FRE-505 captures pattern). One source of truth: the assembled `RouteTraceRow`, never a re-derivation.

**Dedicated index** (acceptance criterion + 7 existing precedents: captains-captures/subagents, slm-requests, user-turn-ratings, …). NOT the shared `agent-logs-*` path; "mirror FRE-547" = mirror the emitter/mapping/dashboard *pattern*.

## ES type discipline (every field walked through the template — the "mappings wrong first pass" trap)

All fields get **explicit** properties AND the template is `"dynamic": false` (codex fix — a
controlled projection index with a known exact schema; any accidental extra field is then stored
-but-not-indexed rather than silently auto-mistyped, the literal "no dynamic-mapping traps"). With
`dynamic: false` the `dynamic_templates` never fire, so the template carries explicit `properties`
only.

| Field | Type | Source (RouteTraceRow) | Emit as |
|---|---|---|---|
| `@timestamp` | date | `created_at` (fallback `now`) | `.isoformat()` (T-separator; `str(datetime)` would fail `strict_date_optional_time`) |
| `trace_id` | keyword | `trace_id` (UUID) | `str(...)` |
| `task_id` | keyword | `task_id` (UUID\|None) | `str(...)`; **omit when None** (turn-level) |
| `session_id` | keyword | `session_id` (UUID\|None) | `str(...)` |
| `topology` | keyword | seam `topology` arg (threaded into `_write_segment_rows`) | str |
| `role` | keyword | derived | `"sub_agent" if task_id else "primary"` (the `(trace_id,task_id)` discriminator) |
| `gateway_label` | keyword | `gateway_label` | str |
| `result_type` | keyword | `orchestration_event` | str — **conscious rename**: ticket Scope names the ES field `result_type`; DTO field is `orchestration_event` |
| `task_type` | keyword | `task_type` | str; **omit when None** (segments) — *beyond the 12; see Open Question 2* |
| `complexity` | keyword | `complexity` | str; **omit when None** (segments) — *beyond the 12; see Open Question 2* |
| `authoritative_cost_usd` | double | `cost_authoritative_usd` | **`float(...)`** (Decimal/`0`→long trap; emit JSON float) |
| `input_tokens` | long | `input_tokens` | `int(...)` |
| `output_tokens` | long | `output_tokens` | `int(...)` |
| `latency_total_ms` | float | `latency_total_ms` | `float(...)`; **omit when None** (segments have none) |

Join keys `keyword`, money `double`, ms `float` — the FRE-537 constraint, all explicit. The
single `ts = row.created_at or datetime.now(timezone.utc)` drives **both** the index-name date and
the `@timestamp` field (codex fix — consistent fallback, no split-brain when `created_at` is None).

**Deliberately NOT projected** (available on the row but out of FRE-548's scoped field list;
trivially addable when FRE-537 panels need them): `model_role`, `decomposition_strategy`,
`sub_agent_count`, `tools_used`, `cost_live_usd`, `cost_reconciled`, `fallback_triggered`,
`error_type`.

## Files

### 1. `docker/elasticsearch/topology-index-template.json` — NEW dedicated template
- `index_patterns: ["agent-topology-*"]`, `priority: 110` (defensive, per the captains pattern; no glob overlap with `agent-logs-*` priority 100 anyway).
- standard settings (1 shard / 0 replicas / best_compression / 5s refresh).
- `mappings.dynamic: false` + explicit `properties` for every field above (no `dynamic_templates`).
- `_meta.description` + `_meta.managed_by: scripts/setup-elasticsearch.sh`.

### 2. `scripts/setup-elasticsearch.sh` — register the template
Add one `put_resource "Index template: agent-topology-template" "/_index_template/agent-topology-template" "$PROJECT_ROOT/docker/elasticsearch/topology-index-template.json"` (PUT replaces — idempotent), alongside the existing template registrations.

### 3. `src/personal_agent/observability/topology/es_projection.py` — NEW projector
```python
TOPOLOGY_INDEX_PREFIX = "agent-topology"

def build_topology_doc(row: RouteTraceRow, *, topology: str) -> dict[str, Any]:
    """Pure: RouteTraceRow + topology -> explicit-typed ES doc (omits None task_id/latency/
    task_type/complexity)."""

def project_route_trace_to_es(row: RouteTraceRow, *, topology: str) -> None:
    """Best-effort, non-blocking projection to agent-topology-YYYY-MM-DD.
    doc_id = f"{trace_id}:{task_id or 'turn'}" (idempotent). No-op without ES/loop."""
```
- The **whole body** of `project_route_trace_to_es` is wrapped in `try/except Exception` (codex
  fix — `schedule_es_index` only guards the *scheduled* write; the synchronous doc-build/casts run
  before it and must not be allowed to raise into the seam).
- `ts = row.created_at or datetime.now(timezone.utc)`; index name `f"agent-topology-{ts.strftime('%Y-%m-%d')}"`, `@timestamp = ts.isoformat()`.
- Reuses `schedule_es_index(index, doc, doc_id=...)` (non-blocking `create_task`, best-effort) from `captains_log.es_indexer` — the lifespan already wired `set_es_indexer(...)` (app.py:501), so no new wiring.
- **Known best-effort gaps (documented, codex):** (a) `ledger.write` silently no-ops when Postgres is unconnected, so ES can carry a row Postgres didn't — acceptable for a best-effort projection; (b) a retry re-projects with `id` upsert (overwrite) while Postgres keeps the first via `ON CONFLICT DO NOTHING` — values are deterministic from the same row, so divergence is immaterial; (c) no ILM/retention on `agent-topology-*` (daily concrete indices) — retention is out of scope for this PR.

### 4. `src/personal_agent/observability/topology/seam.py` — hook the projection
- In `_write_durable_row`, after `await ledger.write(row)`: `project_route_trace_to_es(row, topology=topology)`.
- Thread `topology` into `_write_segment_rows(ctx, topology)`; after each segment `await ledger.write(seg_row)`: `project_route_trace_to_es(seg_row, topology=topology)`.
- Both calls are non-blocking + best-effort, so no new failure path (they cannot raise into the turn).

### 5. `config/kibana/dashboards/` — execution-topology panels  *(scope-gated — see Open Question)*
A new index-pattern `agent-topology-*` (its own saved-object, `@timestamp` time field) + legacy aggs-based visualizations (NOT Lens — FRE-546): topology distribution (`terms topology`), primary-vs-sub-agent row counts (`terms role`), authoritative cost per topology (`sum authoritative_cost_usd` by `topology`), and a `(trace_id, task_id)` data-table drill-in. Per-line JSON validity check.

## Tests (TDD — write first, confirm red)

### `tests/observability/topology/test_es_projection.py` (NEW, unit — no live ES)
- `build_topology_doc` turn-level row → `role=primary`, **no `task_id` key**, `@timestamp` is ISO with `"T"`, `authoritative_cost_usd` is `float`, tokens `int`, `latency_total_ms` present as float.
- `build_topology_doc` segment row (`task_id` set, `latency_total_ms` None) → `role=sub_agent`, `task_id` present (str), **no `latency_total_ms` key**.
- `project_route_trace_to_es` calls `schedule_es_index` with `index="agent-topology-<date>"` and `doc_id="<trace>:<task or turn>"` (monkeypatch `schedule_es_index`, assert args).
- No-op safety: with the indexer absent it must not raise (monkeypatch `schedule_es_index` to confirm it's the only sink; the helper itself already no-ops without a loop/ES).

### `tests/observability/topology/test_seam.py` (extend)
- `test_seam_projects_turn_level_and_segments_to_es`: monkeypatch `project_route_trace_to_es`, ctx with 2 subs → called 3× (1 turn-level `role`-implied + 2 segment rows), each with `topology` passed through.
- Existing seam tests must still pass (projection is additive + best-effort).

### Template JSON validity
`python -c "import json; json.load(open('docker/elasticsearch/topology-index-template.json'))"` and per-line load of any changed `.ndjson`.

## Quality gates
`make test-file FILE=tests/observability/topology/test_es_projection.py` → `make test-file FILE=tests/observability/topology/test_seam.py` → `make test` → `make mypy` → `make ruff-check`/`format` → `pre-commit run --all-files`.

## Post-deploy (Linear comment for master — NOT in PR checklist)
- `scripts/setup-elasticsearch.sh` (or direct `PUT /_index_template/agent-topology-template`) to register the template before the emitter's first write.
- `_field_caps` proof: all 12 fields resolve at intended type on the first `agent-topology-*` index (join keys keyword, money double, ms float).
- (If dashboard in-scope) import the NDJSON; confirm objects resolve + panels populate after real turns.

## Open questions for approval
1. **Dashboard scope.** The emitter + dedicated index + template + tests is the core (fully
   unit-testable now). The 4 Kibana panels (acceptance #4) can only be verified against live data
   (post-deploy) and FRE-537 is the dedicated dashboard ticket this unblocks. **Recommend: ship the
   emitter+index+template in this PR; build the panels as a follow-up / under FRE-537** — keeps this
   PR tight + verifiable and avoids shipping unverifiable dashboard JSON. The `build_topology_doc`
   unit test pins the exact doc schema as a stable contract FRE-537 consumes (codex suggestion).
2. **Field set.** Ticket Scope lists 12 fields; I added `task_type` + `complexity` (turn-level,
   omit-None) because the ticket *Problem* names them as missing cognitive-work fields and FRE-537
   panels will want topology×task_type. Say if you want strictly the scoped 12.

## Halt-condition check
Single coherent feature (emitter + dedicated index + template). No historical rows dropped. No ADR-phase bundling. No expected mypy regression. One phase = one PR.
