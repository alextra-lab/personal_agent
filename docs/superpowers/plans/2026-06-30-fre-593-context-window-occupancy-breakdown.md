# FRE-593 — Per-request context-window occupancy breakdown

**Ticket:** FRE-593 (Approved, Tier-2:Sonnet) · Project: Observability Foundation (L0)
**Backing ADRs:** ADR-0090 (telemetry surface / guarded-dynamic mapping discipline), ADR-0092 (lineage).
Empirical trigger feeding ADR-0096 (memory access model).

## Problem

`request_gateway/budget.py` already computes the *total* context tokens
(`_total_context_tokens`, lines 41-65) and emits only a scalar (`total_tokens`) plus
`has_memory`/`has_tools` booleans on the `context_budget_applied` structlog event
(lines 270-283). We cannot see *how* the window is occupied — how many tokens are
memory enrichment vs tool definitions vs preserved reasoning. That breakdown is the
empirical input ADR-0096 needs.

## Acceptance criteria (from ticket / ADR-0090)

1. **AC1** — ES doc for the `context_budget_applied` event carries a per-turn
   breakdown `{memory_tokens, tool_tokens, reasoning_tokens, total}`.
2. **AC2** — every new field walked through the index-template mapping discipline
   (ADR-0090 guarded-dynamic): explicitly pinned, no float→long / keyword-`ignore_above`
   trap. `_field_caps` confirms types live.
3. **AC3** — a Kibana view shows composition over time.

## Design decisions

- **Grouped `object` mapping, not flat fields.** Emit `context_occupancy: {memory_tokens,
  tool_tokens, reasoning_tokens, total}`. Rationale: the flat field `total` is already
  claimed by consolidation events (template line 145, `integer`), and `total_tokens`
  already exists on this very event. A grouped object (a) matches the acceptance's
  literal `{...}` shape, (b) groups the breakdown unambiguously, (c) avoids the `total`
  collision. Precedent: the `quality_alert` object (template lines 179-185). NB this is
  an ES `object` mapping (flattened dotted leaves), **not** `nested` — `nested` has
  different query semantics and is not wanted here.
- **Token magnitudes → `long`** (matches `input_tokens`/`output_tokens`/`total_tokens`).
  These are genuine integer counts; pinning them explicitly defeats any dynamic
  inference and satisfies ADR-0090's "every known numeric field explicit" bar.
- **Post-trim occupancy.** Compute the breakdown on the *final* (post-trim)
  `messages`/`memory_context`/`tool_definitions`, consistent with the existing
  `total_tokens` value on the same event — it describes what the model actually sees.
- **`total` key == existing `total_tokens`.** Same number, carried inside the object so
  the breakdown is self-contained for a single-doc reader / the dashboard.
- Categories mirror `_total_context_tokens`'s own decomposition:
  - `reasoning_tokens` = estimate of joined `reasoning_content` across messages.
    **Zero-fills on non-thinking model turns** (no `reasoning_content`) — expected, not a bug.
  - `memory_tokens` = estimate of joined `str(item)` over `memory_context` (0 if None).
  - `tool_tokens` = estimate of joined `str(tool)` over `tool_definitions` —
    **tool *definitions* only.** Tool *results* return as messages and fall into the
    residual, not here. (0 if None.)
  - `total` = `_total_context_tokens(...)` (includes message `content` too, so
    `total ≥ memory+tool+reasoning`). The **residual** (`total − the three`) is
    everything else the model sees: system prompts, conversation history, user/assistant
    message text, and tool *results*. This is documented in the helper docstring so
    readers don't mistake `tool_tokens` for all tool context.
- **Post-trim only — known limitation.** The breakdown reflects the *final* context, so
  memory/tools/history dropped by trimming vanish from the composition. Steady-state
  occupancy dashboards (this ticket's goal) are well served; *overflow forensics*
  ("what pressure forced eviction, and what got evicted") is **not** answerable from this
  field alone — that is a possible ADR-0096 follow-up, out of scope here.

## Steps

### 1. `src/personal_agent/request_gateway/budget.py` — pure helper
Add `_context_occupancy(messages, memory_context, tool_definitions) -> dict[str, int]`
returning the four keys. Pure, no I/O, fully testable. Reuses `estimate_tokens` and
`_total_context_tokens`.

### 2. `budget.py` — emit on the event
In `apply_budget`, right before the `logger.info("context_budget_applied", ...)`,
compute `occupancy = _context_occupancy(messages, memory_context, tool_definitions)`
(post-trim values) and add `context_occupancy=occupancy` to the event kwargs.
ADR-0074 identity threading: this is an existing event already carrying `trace_id`/
`session_id`-adjacent context; no new `MERGE`/`bus.publish`, so no allowlist change.

### 3. `docker/elasticsearch/index-template.json` — explicit mapping
Add under `properties`:
```json
"context_occupancy": {
  "type": "object",
  "properties": {
    "memory_tokens":    { "type": "long" },
    "tool_tokens":      { "type": "long" },
    "reasoning_tokens": { "type": "long" },
    "total":            { "type": "long" }
  }
}
```
Update the `_meta.description` to note the FRE-593 occupancy object (guarded-dynamic).

### 4. Tests (TDD — write first, watch fail)
- `tests/personal_agent/request_gateway/test_budget.py` → `TestContextOccupancy`:
  - memory-only context → only `memory_tokens` > 0, others 0, `total` ≥ memory.
  - tool-only → only `tool_tokens` > 0.
  - reasoning-only (message with `reasoning_content`) → only `reasoning_tokens` > 0.
  - all three present → all > 0; `total` ≥ sum of the three.
  - empty context → all four 0 (or total == content-only).
  - **emit test** via `structlog.testing.capture_logs`: `apply_budget` event
    `context_budget_applied` carries `context_occupancy` with the 4 int keys and
    `context_occupancy["total"] == event["total_tokens"]`.
- `tests/scripts/test_es_templates.py` → `test_logs_context_occupancy_object_explicit`:
  asserts the object is explicit with the 4 `long` sub-fields (mirrors the
  `test_logs_adr0092_turn_status_session_fields_explicit` precedent).

### 5. Kibana view — `config/kibana/dashboards/context_occupancy.ndjson`
A Lens stacked-area "Context window occupancy over time": x = date_histogram on
`@timestamp`, stacked sum series on `context_occupancy.memory_tokens`,
`context_occupancy.tool_tokens`, `context_occupancy.reasoning_tokens`, filtered to
`event_type: context_budget_applied`. References the existing `agent-logs-pattern`
index-pattern. Add a README.md bullet. (Import is a master post-deploy step.)

### 6. Docs
README bullet in `config/kibana/dashboards/README.md`.

## Verification / quality gates
- `make test-file FILE=tests/personal_agent/request_gateway/test_budget.py`
- `make test-file FILE=tests/scripts/test_es_templates.py`
- `make test` (module then full) · `make mypy` · `make ruff-check` + `ruff-format` ·
  `pre-commit run --all-files`
- Kibana ndjson validates as JSON-per-line (python json.loads each line).

## Post-deploy runbook (for master — NOT in PR checklist)
1. Re-register the agent-logs template: `./scripts/setup-elasticsearch.sh` (additive,
   no type change to existing fields → standing-approval class).
2. Import the dashboard: `./config/kibana/import_dashboards.sh`.
3. Fire one real turn; confirm `_field_caps`:
   `curl -s 'localhost:9200/agent-logs-*/_field_caps?fields=context_occupancy.*'`
   → each sub-field `type: long`.
4. Open the new dashboard → composition-over-time renders (AC3).

## Out of scope
- No change to trimming behaviour. No new ADR. No pre-trim breakdown (post-trim only).
