# ADR-0074: End-to-End Traceability and Observability Joinability

**Status:** Proposed
**Date:** 2026-05-22
**Issue:** FRE-376
**Supersedes:** —
**Related:** ADR-0072 (test/prod substrate isolation), ADR-0073 (cross-fact constraint layer)

## Context

While preparing the FRE-374 backfill replay on 2026-05-22, an audit of session attribution surfaced a foundational observability gap. The instrumentation surface is rich — 30+ event types in Elasticsearch, structured Captain's Log captures, durable Postgres tables, Neo4j write events — but the **join keys are missing** at multiple layers:

- `api_costs.trace_id` is NULL on every row (4,077 / 4,077). There is no link from a cost record to the session or request that produced it.
- The `sessions` table records the conversation content but has no `model`, `model_config_path`, or per-message model attribution.
- `LocalLLMClient` emits `model_call_completed` events without `session_id` or `trace_id` — local model usage is invisible to any cross-system query.
- `LiteLLMClient` emits `litellm_request_start` events with `trace_id` and `model` but **without `session_id`**.
- Tool call events (`tool_call_started`, `tool_call_completed`) carry `trace_id` inconsistently and rarely carry `session_id`.
- `messages[].metadata` in `sessions.messages` records only `{"source": "service.app"}` — not the model that produced each assistant message.

The result: months of historical evals cannot be attributed to specific models, costs cannot be allocated to sessions, behavioral debugging cannot follow a single request across the stack, and the FRE-374 replay cannot reconstruct which sessions used cloud vs. local inference.

This is not a missing-instrumentation problem. **It is a missing-foreign-key problem.**

## Decision

Adopt six invariants for all observability streams going forward. Enforce at write time, not as convention. Convention has already failed.

### I1 — Every span carries the full identity tuple

`(session_id, trace_id, span_id, parent_span_id)` is required on every event emitted to any stream (structlog → ES, Redis bus, Postgres, Neo4j, Captain's Log). Optional means "the system doesn't know if its data is joinable."

### I2 — Both model paths emit the same event shape

`LiteLLMClient` and `LocalLLMClient` emit identical event shapes: `model_call_started` and `model_call_completed` with `model`, `role`, `session_id`, `trace_id`, `span_id`, `parent_span_id`, `input_tokens`, `output_tokens`, `latency_ms`. A request handler that switches between cloud and local cannot tell the difference in the resulting telemetry. A shared `ModelClientTelemetry` mixin enforces the contract.

### I3 — Per-message model attribution in `sessions`

Every assistant message persisted to `sessions.messages[]` records:
- `model` — exact model identifier (e.g. `anthropic/claude-sonnet-4-6`, `unsloth/qwen3.6-35-A3B`)
- `model_role` — `primary` / `extractor` / `captains_log` / `insights` / etc.
- `model_config_path` — the resolved YAML path active when the message was produced

Plus, on session creation, `sessions.model_config_path` and `sessions.primary_model_at_creation` columns record the active config. Already-stored messages remain as-is; new messages cannot be persisted without these fields.

### I4 — Cost records are NOT NULL on identity

`api_costs.session_id` and `api_costs.trace_id` become `NOT NULL` columns. The cost tracker rejects writes that don't carry them, rather than silently inserting NULL. A schema migration adds the columns and a forward-only check constraint. Existing NULL rows are dropped or quarantined (they are already unattributable).

### I5 — Memory writes carry origination

`(:Turn)`, `(:Entity)`, `(:Relationship)`, and future `(:DescriptionVersion)` nodes carry as properties:
- `originating_trace_id`
- `originating_session_id`
- `extractor_model` (which model wrote the description / created the relationship)

Asking "which model produced this entity description" becomes a single property read on the node.

### I6 — TraceContext is required, not optional

The `TraceContext` type loses its `None` default on internal APIs. Functions that need to operate without a user-facing trace (boot, scheduler ticks, periodic monitors) get an explicit `SystemTraceContext` factory that produces a valid identity tuple tagged with `kind="system"`. The type checker — not runtime checks — enforces presence.

## Enforcement

Convention alone has already failed. Concrete mechanisms:

1. **Database constraints:** `NOT NULL` on `api_costs.session_id` and `api_costs.trace_id`. `CHECK` constraint on `sessions.messages` requiring `model` field when `role = 'assistant'`.
2. **Code lint (pre-commit):** `scripts/check_identity_threaded.py` fails when new code calls `log.info(...)`, `bus.publish(...)`, `session.run(<Cypher MERGE|CREATE>, ...)`, or other event sinks without `trace_id` and `session_id` in the kwargs. Explicit opt-out: `# trace-allow: <reason>` for genuine exceptions (startup events, system-tick monitors).
3. **Contract test:** `tests/contracts/test_identity_threaded.py` parses the codebase, finds every event-emit site, and asserts the required identity keys are passed at the call site (AST-level check, not runtime).
4. **Joinability probe:** `scripts/monitors/joinability_probe.py` selects one random `session_id` from the last 24 hours and walks every backend (Postgres ↔ ES ↔ Neo4j ↔ Redis). Asserts that for every row, every related row exists and matches. Runs in CI as a smoke test with a stub session, and as a periodic monitor against prod.

## Implementation phases

Each phase is independently shippable.

**Phase 1 — Schema and write-time enforcement (I4 + part of I3):**
- Migration: add `model_config_path`, `primary_model_at_creation` columns to `sessions`.
- Migration: enforce `model` + `model_role` + `model_config_path` in each assistant message in `sessions.messages[]` via service-layer validation (not DB check, since JSONB).
- Migration: `api_costs.session_id` and `api_costs.trace_id` `NOT NULL` with `DELETE FROM api_costs WHERE trace_id IS NULL OR session_id IS NULL` cleanup of pre-cutoff rows.
- `CostTracker.record()` raises `MissingIdentityError` on missing fields.

**Phase 2 — Equalize the model client paths (I2):**
- `ModelClientTelemetry` mixin defines the canonical event shape.
- `LocalLLMClient` and `LiteLLMClient` adopt it.
- Existing event-name compatibility maintained (no breaking changes for downstream consumers).

**Phase 3 — Thread session/trace through every emit site (I1):** ✅ **Shipped**
- `scripts/check_identity_threaded.py` AST lint enforces ADR-0074 §I3 / §I5
  on every commit (wired into `.pre-commit-config.yaml`).
- All 21 `bus.publish` sites use typed Pydantic `Event` payloads that enforce
  identity at construction.
- All Cypher `MERGE` writes on `:Turn` and `:Entity` carry
  `originating_trace_id`, `originating_session_id`, and (for extracted
  entities) `extractor_model` per §I5.
- `executor.py` orchestrator step boundary emits `STEP_PLANNING_STARTED` /
  `STEP_PLANNING_COMPLETED` with the full identity tuple — `MODEL_CALL_*` is
  now exclusively a model-client event.
- Phase 2 back-compat aliases removed (`model_id`, `prompt_tokens`,
  `completion_tokens`, `litellm_request_start`, `litellm_request_complete`).
  ES index template + Kibana dashboard generator + 4 NDJSON dashboards +
  eval harness all re-pointed to canonical field names atomically.
- 200+ structlog kwargs now carry `trace_id` (orchestrator, memory, gateway,
  service, llm_client, captains_log, second_brain, brainstem, mcp, transport,
  events, telemetry, cost_gate, config, tools, storage). Background ops mint
  a `SystemTraceContext` per §I4 rather than passing `None`.
- ~370 sites legitimately allowlisted with per-function reasons (sensor
  hardware polls, FastAPI lifespan, MCP gateway init, protocol contract
  boundaries that lack ctx without a wider refactor).

**Deferred to follow-up FRE:**
- `captains_log/capture.py:61` (`CaptureEntry` dataclass field names) —
  internal substrate schema rename triggers Captain's Log replay.
- Tightening the optional `trace_id: str | None = None` kwargs introduced
  during this phase to the non-optional `SystemTraceContext`/`TraceContext`
  parameters that Phase 4 (I6) calls for.

**Phase 4 — Type system enforcement (I6):**
- `TraceContext: ...` (non-optional) on internal APIs.
- `SystemTraceContext` factory for boot/scheduler/monitor paths.
- mypy strict mode catches every site that passes `None`.

Phase 4 ships in two slices:
- **4a (shipped 2026-05-22):** `SystemTraceContext.new(source)` factory; `TraceContext.kind` field (`"user"` / `"system:<source>"`); `LocalLLMClient`, `LiteLLMClient`, `LLMClientProtocol`, all delegation adapters (`ClaudeCodeAdapter`, `CodexAdapter`, `GenericMCPAdapter`), and `tools.executor._check_permissions` require non-optional `trace_ctx`; ad-hoc `TraceContext.new_trace()` migrated to `SystemTraceContext.new(...)` in `captains_log.feedback`, `captains_log.reflection`, `gateway.knowledge_api`, `second_brain.session_summary`, `second_brain.entity_extraction`; `LiteLLMClient` cost-record + budget-reserve paths drop their `if trace_ctx else …` defensive branches; mypy strict clean.
- **4b (pending):** Tighten tool executor signatures (`bash`, `read`, `write`, `run_python`, `web_search`, `perplexity`, `context7`, `linear.*`, `read_skill`) — currently keep `ctx: TraceContext | None = None` because their test surface (~57 invocations) needs a coordinated update; remove the `<truncated: no ctx>` codepath from `bash_executor` once the signature is required.

**Phase 5 — Probes and CI gates:**
- Joinability probe (one random session, full walk).
- Pre-commit lint.
- Contract test.

## Consequences

**Positive:**
- Behavioral debugging follows a single request across the entire stack.
- Costs allocate to sessions and to models, so eval cost attribution becomes precise.
- "Which model produced this output / wrote this entity / made this tool call" is a single property read.
- Future evals can A/B compare models cleanly because every artifact is attributable.
- The FRE-374 replay (and future replays) can determine per-session model context.

**Negative / tradeoffs:**
- Pre-cutoff `api_costs` rows (the existing 4,077 NULL-trace rows) are dropped or quarantined as part of Phase 1. Acceptable — they were already useless for attribution.
- Every internal API that doesn't currently take `TraceContext` must be updated. Mechanical but widespread.
- Pre-commit lint will reject new code that doesn't thread identity. Expected friction; the right friction.
- A small ongoing cost: every event payload carries ~4 additional UUID fields. Negligible compared to the bytes already in messages and prompts.

**Explicitly not in scope:**
- Backfilling history. User has declared this out of scope.
- Reconstructing the model attribution of pre-cutoff sessions. Impossible without `trace_id` in `api_costs`.

## Verification

- Joinability probe selects one random `session_id` from the last 7 days and:
  - Joins to all messages, all `api_costs` rows for that session, all ES events with that trace_id or session_id, all Neo4j Turn nodes with that originating_session_id.
  - Asserts that for every row, every related row exists and matches.
  - Reports orphans as failures.
- Acceptance criterion for Phase 5: zero orphans across 100 random sample sessions in prod.
- Per-phase verification:
  - Phase 1: a unit test asserts `CostTracker.record()` raises on missing identity; the existing `api_costs` table contains no NULL rows post-migration.
  - Phase 2: an integration test pings both `LocalLLMClient` and `LiteLLMClient` and asserts identical event field shapes.
  - Phase 3: an AST-level audit shows zero `log.*` / `bus.publish` sites without identity kwargs.
  - Phase 4: `mypy --strict` passes with `TraceContext` non-optional on internal APIs.
  - Phase 5: pre-commit hook + contract test pass; joinability probe green for 7 consecutive days in production.
