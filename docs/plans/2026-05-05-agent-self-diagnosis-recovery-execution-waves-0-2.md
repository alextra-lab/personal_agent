# Recovery Plan — Wave 0–2 Execution

**Date**: 2026-05-05
**Source doc**: `docs/plans/2026-05-05-agent-self-diagnosis-recovery-plan.md`
**Scope of this file**: Waves 0–2 only (freeze, harness + ES survey, infra/memory canaries). Waves 3–5 (baseline runs, recovery profile, permanent decisions) are deferred to a follow-up plan written after Wave 2 produces evidence.

---

## Context

The source doc identifies a system-level regression risk spanning five interacting layers: tool-loop gates, skill injection, within-session compression, memory-write pipeline, and infrastructure readiness. Each layer received recent changes (FRE-263, FRE-282, FRE-251, FRE-302–307, ADR-0065, `39cde53` startup wait fix). Together they may have collapsed the agent's ability to inspect itself, retain context, and write/retrieve memories — even though no individual change is obviously broken.

This plan executes the source doc's evidence-first discipline: freeze risky moves, build a reproducible harness, query existing telemetry, then run two canaries (infrastructure + memory). Permanent design changes wait until those canaries localize the failures.

## Decisions made during planning

| Decision | Choice | Rationale |
|---|---|---|
| Compression budget | Fix geometry first, then baseline | `context_window_max_tokens=2048` with `within_session_min_tail_tokens=2000` leaves ~48 tokens for everything except the tail. Running a baseline against this is wasted evidence. |
| Harness surface | `scripts/eval/recovery_harness.py` driving `/chat` API + `make eval-recovery RUN=<id>` | Single command produces files an agent can re-read; controls `session_id` and `trace_id` directly; reusable for memory canary. |
| Survey first | Yes, before any canary | Existing ES/Neo4j data may already localize the failure and shrink Wave 2. |
| Recovery profile (deferred to Wave 4) | Will ship as `AGENT_RECOVERY_PROFILE=true` flag bundle, then promote to brainstem `MODE=diagnostic` after validation | Trivially revertible during the most fragile window; brainstem mode is the right long-term home but not the right place to first land an unproven config. |
| Plan scope | Wave 0–2 only | Smallest reversible commitment; matches the source doc's evidence-first principle. |
| Master Plan freeze ownership | In-effect on plan landing; no separate Linear gate | Meta-level operating rule, not a feature. |
| Wave 2 rescope (added 2026-05-05 post-baseline) | Drop the planned canary_infra / canary_memory / canary_cleanup scripts; treat the Wave 1.2 baseline as fulfilling Wave 2's detection role; point Wave 2 work at FRE-323 (consolidator skip) and FRE-324 (synthesis stub) | The harness baseline (`baseline-2026-05-05-v2/`) localized the failure to a single line in `consolidator.py:138` in 16 minutes. Building the planned 9-stage canary script would discover the same line; that is duplicate work in an active recovery window. |

---

## Wave 0 — Freeze and immediate fixes

### 0.1 Master Plan freeze (no-code change)
- **Edit** `docs/plans/MASTER_PLAN.md`: prepend a banner pointing at this file and the source doc (`docs/plans/2026-05-05-agent-self-diagnosis-recovery-plan.md`). Do not start new Master Plan items until Wave 2 canaries pass.
- **Comment** on the FRE-265 Linear issue: link this plan, mark blocked. Do **not** change state.
- **Operating rules in effect** until Wave 2 closes:
  - No further loop-gate tightening.
  - No further context-budget tuning beyond 0.2 below.
  - No memory-pipeline tuning of any kind (writes or retrieval).
  - No multi-subsystem PRs.
  - No legacy-tool deletion (FRE-265).
  - Read-only diagnostic capability is preferred over cost reduction during the window.

### 0.2 Compression geometry fix (the only code change in Wave 0)
- **File**: `src/personal_agent/config/settings.py:446`, `:487`.
- **Change A** — `context_window_max_tokens` default: from `2048` to the real usable model context for the active primary-agent profile. Verify the qwen3.6-35B-A3B context (commonly 32k or 128k); pick a conservative usable value (e.g., `24000` for 32k, leaving 8k headroom for response tokens) and document the choice in the field description.
- **Change B** — `within_session_min_tail_tokens` default: replace fixed `2000` with a value derived as a fraction of `context_window_max_tokens`. Two acceptable shapes:
  1. Express as a ratio field `within_session_min_tail_ratio: float = 0.25` and compute the absolute floor in `within_session_compression.py:257`.
  2. Keep `within_session_min_tail_tokens` but raise the default proportionally and add a Pydantic validator that rejects configs where `within_session_min_tail_tokens > 0.5 * context_window_max_tokens`.
  Prefer (1) — it's drift-proof.
- **Add validator** rejecting configs that allocate <500 tokens to the head+middle region: `assert context_window_max_tokens - effective_min_tail_tokens >= 500`.
- **Tests**: `tests/personal_agent/config/test_settings.py` (or wherever settings are tested today — confirm during execution). Add cases:
  - validator rejects pathological geometry,
  - effective tail is computed correctly from ratio,
  - no regression in `compression_manager.py` and `within_session_compression.py` consumers.
- **Existing consumers to re-check**: `orchestrator/executor.py:1030,1247,1260`, `orchestrator/compression_manager.py:93`, `orchestrator/within_session_compression.py:189,242,257`. All read `settings.*` directly so the change propagates without code edits there — but the test file should exercise them with the new defaults.

### 0.3 Explicit non-changes
This wave does **not** touch loop gates, role caps, skill injection, memory pipeline, cost gates, or governance. Anyone who touches those during Wave 0 is violating the freeze.

---

## Wave 1 — Harness and ES/Neo4j survey

### 1.1 Survey existing telemetry first
- **New script**: `scripts/eval/recovery_survey.py`. Reuses `QualityMonitor` and `TelemetryQueries` rather than reinventing — the survey is a thin orchestration + reporting layer.
- **A. Pipeline-flow counts** (last 7 days, scoped to the current environment):
  - ES counts of: `entity_extraction_started`, `entity_extraction_complete`, `entity_extraction_failed`, `BudgetDenied` (grouped by role), `request.captured` events, `memory_service_initialized` startup events, `elasticsearch_logging_enabled` startup events, `event_bus_ready` startup events, scheduler/consumer startup logs.
  - Capture-vs-extraction gap: `count(captures) - count(extraction_started)`. A non-trivial gap means the scheduler is dropping work.
  - Extraction-success ratio: `count(complete) / count(started)`. Below ~80 % means extraction itself is failing.
  - Cost-gate denial ratio for the `entity_extraction` role.
- **B. Quality reports** — call existing methods directly:
  - `QualityMonitor.check_entity_extraction_quality(days=7)` → entities/conversation ratio, duplicate rate, name length distribution, failure rate.
  - `QualityMonitor.check_graph_health()` → graph topology metrics.
  - Render the `QualityReport` and `GraphHealthReport` into the markdown output verbatim, plus a thresholds section that flags: ratio < 1.0 (under-extraction), duplicate_rate > 0.1 (dedup/embedding issue), failure_rate > 0.2 (model crashing).
- **C. Model identity audit** — read `config/models.yaml`:
  - Log which model fills `entity_extraction_role`, `captains_log_role`, `insights_role`. **Flag if all three share one model** (current state: `gpt-5.4-nano` for all → single point of failure).
  - Log embedding model id, endpoint, and dimensions from the `embedding` entry.
- **D. Embedding health probe** (live, three-step):
  - Call the configured `/v1/embeddings` endpoint with three test strings (e.g., "ultramarine color", "diagnostic recovery plan", "unrelated banana"). Verify response is the configured dimensionality and is non-zero.
  - Compute pairwise cosine similarity between the three. If all ≥0.95 (collapsed embeddings) or any pair ≤0.0 (degenerate), flag as critical.
  - ES count of `zero_embedding` events from `protocol_adapter.py` over last 7 days. Any non-zero count is a smoking gun.
  - Neo4j sample: `MATCH (e:Entity) WHERE e.embedding IS NOT NULL RETURN e.embedding LIMIT 50` — check fraction with all-zero or null embeddings, and report mean pairwise cosine similarity (if everything is ~0.99 similar to everything else, the model has collapsed).
- **Output**: `telemetry/evaluation/EVAL-agent-self-diagnosis/survey-2026-05-05/report.md` — sections A/B/C/D plus a "likely localized failures" summary.
- **Hard rule**: survey script must fail loudly on missing ES indices, unreachable Neo4j, unreachable embedding endpoint, or missing `entity_embedding` vector index (no silent zeros).
- **Decision after survey**: if a single layer is clearly failing (e.g., zero extractions in 7 days, or collapsed embeddings, or saturated cost-gate denials), narrow the canaries below to confirm-and-fix that layer; document the narrowing in the survey report.
- Confirm exact ES index names and event field names by reading `src/personal_agent/telemetry/` and `src/personal_agent/captains_log/` first.

### 1.2 Harness scaffolding
- **New files**:
  - `scripts/eval/recovery_harness.py`
  - `telemetry/evaluation/EVAL-agent-self-diagnosis/prompts.yaml`
  - `telemetry/evaluation/EVAL-agent-self-diagnosis/README.md`
- **Make targets** (added to `Makefile`): `eval-recovery-survey`, `eval-recovery RUN=<id> [PROFILE=baseline|recovery] [PROMPT=<id>]`, `canary-infra`, `canary-memory UUID=<uuid>`.
- **Harness behaviour** (per prompt):
  1. POST to `http://localhost:9000/chat` with optional `session_id` reuse for multi-turn canaries.
  2. Capture `trace_id` from response (verify the `/chat` endpoint surfaces it; if not, that's a one-line addition to `service/app.py` — confirm during execution).
  3. Wait ≤5 s for ES indexing.
  4. Pull the trace from ES and extract:
     - skill docs injected (look for `skill_injected` events from `request_gateway`),
     - tool calls requested vs executed,
     - loop-gate decisions,
     - forced-synthesis events,
     - compression events (head/middle/tail token counts in vs out),
     - memory_context size,
     - Captain's Log capture id,
     - entity extraction outcome.
  5. Query Neo4j for `Turn`/`Entity`/relationship writes within the trace's wall-clock window.
  6. Render `telemetry/evaluation/EVAL-agent-self-diagnosis/<run-id>/<prompt-id>/report.md`.
  7. After all prompts: render `summary.md` with pass/fail per gate.
- **`prompts.yaml` initial set** (one entry per source-doc canary):
  - `self_diagnosis_recent_regression`
  - `es_log_investigation`
  - `neo4j_memory_inspection`
  - `memory_canary_recall` (multi-turn)
  - `long_diagnostic_session`
  - `primitive_tool_with_implied_skill`
  - `service_startup_health_inspection`
  - `loop_prone_query_refinement`
- **No baseline run yet** — the harness exists but the scored baseline is Wave 3 work.

---

## Wave 2 — Superseded by the Wave 1.2 baseline

> **Status (2026-05-05):** the Wave 1.2 harness baseline (`baseline-2026-05-05-v2/`) already produced what the original Wave 2 was designed to discover. Both planned canaries are redundant; the work shifts to fixing what the baseline localized.

### Why the original Wave 2 is moot

| Original Wave 2 step | What the baseline already showed |
|---|---|
| 2.1 Infrastructure readiness canary | `service_startup_health_inspection` prompt called `GET /health` and confirmed db / ES / Neo4j / second_brain / event_bus / MCP all connected. ES write path fired (`capture_written` events landed). Redis consumer received the event (`event_request_captured_received`). Neo4j read/write worked (the post-run survey query returned 1566 Turn nodes from prior activity). |
| 2.2 Memory write pipeline canary | `memory_canary_recall` (with `new_session: true` on turn 2) localized the failure precisely: `consolidation_processing_capture` fires once, then `consolidation_skipped_already_consolidated` fires 5 times, and **no** Turn or Entity is ever written. The 9-stage canary script would have discovered the same single line — `consolidator.py:138` — by running the same prompt. |

The baseline produced this evidence in 16 minutes against the live system. Building canary scripts to confirm what the baseline already proved would not change the next action.

### What the baseline did NOT cover (carry-overs)

- **Sustained regression detection.** The baseline is a one-shot snapshot. The harness is the right tool for ongoing detection — wire it into a recurring schedule once Wave 2.A and 2.B (below) land.
- **Embedding correctness on freshly-written entities.** Stored embeddings are non-degenerate per the survey, but no entity from the baseline's traces actually exists in Neo4j to verify. Re-check after FRE-323 lands.

### Wave 2.A — Fix `consolidator.turn_exists` skip (FRE-323) ✅ Done 2026-05-05

- **Linear**: [FRE-323](https://linear.app/frenchforest/issue/FRE-323) — PR #16.
- **Root cause**: `ON CREATE SET e.visibility = $visibility` was placed *after* an unconditional `SET` clause in the entity-loop Cypher inside `create_conversation` and in `create_entity`. Cypher's grammar requires `ON CREATE SET` to follow `MERGE` before any `SET`; every Entity write was rejected with `CypherSyntaxError`. The Turn `MERGE` (a separate `session.run`, auto-committed) still landed, so `turn_exists(trace_id)` correctly returned True and subsequent consolidation passes skipped the trace — leaving no Entity nodes or `DISCUSSES` edges. Introduced by FRE-229 commit `9f04114`.
- **Fix**: reordered `ON CREATE SET` ahead of `SET` in both query sites. Two structural-ordering regression tests added.
- **Acceptance verified**: `fre323-postfix-v2` run — Turn `6a836724` has `Ultramarine` + `Recovery Plan` Entity nodes with `DISCUSSES` edges; `Ultramarine.description = "The diagnostic color specified for the recovery plan in the conversation."`

### Wave 2.B — Fix synthesis stub after one tool call (FRE-324)

- **Linear**: [FRE-324](https://linear.app/frenchforet/issue/FRE-324) (Tier-2:Sonnet, Needs Approval, **blocked on FRE-323**).
- **Scope**: orchestrator returns "I reached my tool-use limit before completing a synthesis" after a single `search_memory` call returning success. Find the cap or one-shot synthesis path that fires and let the model produce a real answer.
- **Acceptance**: with FRE-323 fixed, `make eval-recovery RUN=synthesis-fix-verify --prompt memory_canary_recall` turn 2 contains the canary's distinctive content (`ultramarine`) in the response.

### Wave 2.C — Sweep follow-ups (lower priority, parallelizable)

- [FRE-319](https://linear.app/frenchforest/issue/FRE-319) — model_config drift audit (Tier-3).
- [FRE-320](https://linear.app/frenchforest/issue/FRE-320) — skill-injection test rot (Tier-3).
- [FRE-321](https://linear.app/frenchforest/issue/FRE-321) — primitive-flag default drift (Tier-3).
- [FRE-322](https://linear.app/frenchforest/issue/FRE-322) — Conversation→Turn schema drift in QualityMonitor (Tier-3).

These can land in any order; none block 2.A or 2.B.

### Decision gate for Waves 3–5

- 2.A and 2.B both pass acceptance → write the Wave 3–5 follow-up plan.
- 2.A fails to land → escalate to Tier-1 with the FRE-323 evidence; the recovery freeze remains in effect.
- The recurring-harness scheduler design (sustained regression detection) belongs to the follow-up plan, not Wave 2.

---

## Critical files

**Read first to confirm exact paths during execution**:
- `src/personal_agent/config/settings.py` (lines 440–500) — compression settings.
- `src/personal_agent/orchestrator/within_session_compression.py:257` — consumer of `within_session_min_tail_tokens`.
- `src/personal_agent/orchestrator/compression_manager.py:93` — consumer of threshold ratio.
- `src/personal_agent/service/app.py` — confirm `/chat` surfaces `trace_id` in its response body.
- `src/personal_agent/telemetry/` — ES index names and event field names used by canaries.
- `src/personal_agent/captains_log/` — capture API and direct-trigger fallback.
- `src/personal_agent/memory/` — `MemoryService` API surface for the synthetic Neo4j write.
- `src/personal_agent/memory/embeddings.py` — embedding generation, model identity loading.
- `src/personal_agent/memory/dedup.py` — entity_embedding vector index used during writes.
- `src/personal_agent/memory/protocol_adapter.py:254` — query-side embedding generation and `zero_embedding` short-circuit.
- `src/personal_agent/second_brain/quality_monitor.py:162` — `check_entity_extraction_quality` and `check_graph_health`; reuse from survey rather than reinventing.
- `src/personal_agent/second_brain/entity_extraction.py` — extraction pipeline; survey + canary inspect its outputs.
- `src/personal_agent/events/` — Redis stream and consumer-group names.
- `src/personal_agent/brainstem/scheduler.py:598` — periodic quality-monitor invocation; survey can mirror its calling convention.
- `config/models.yaml` — `entity_extraction_role`, `captains_log_role`, `insights_role`, and `embedding` model definitions.

**New files**:
- `scripts/eval/recovery_survey.py`
- `scripts/eval/recovery_harness.py`
- `scripts/eval/canary_infra.py`
- `scripts/eval/canary_memory.py`
- `scripts/eval/canary_cleanup.py`
- `telemetry/evaluation/EVAL-agent-self-diagnosis/prompts.yaml`
- `telemetry/evaluation/EVAL-agent-self-diagnosis/README.md`

**Modified**:
- `docs/plans/MASTER_PLAN.md` — pause banner.
- `Makefile` — `eval-recovery-survey`, `eval-recovery RUN=<id>` targets. (The previously planned `canary-infra`, `canary-memory`, `canary-cleanup` targets are out of scope per the 2026-05-05 rescope.)
- `src/personal_agent/config/settings.py` — geometry fix and validator.
- `tests/personal_agent/config/test_settings.py` (or equivalent) — new tests covering the geometry constraints.

---

## Verification

**Wave 0**:
- `uv run pytest tests/personal_agent/config/` — passes; the new validator rejects pathological geometry.
- `uv run mypy src/personal_agent/config/` — passes.
- `uv run ruff check src/personal_agent/config/` — passes.
- `MASTER_PLAN.md` opens with the pause banner.

**Wave 1**:
- `make eval-recovery-survey` produces `telemetry/evaluation/EVAL-agent-self-diagnosis/survey-2026-05-05/report.md` with non-empty counts (or loud failure on missing index).
- `make eval-recovery RUN=test-dryrun PROMPT=primitive_tool_with_implied_skill` writes a single per-prompt report and exits 0.

**Wave 2** (rescoped — see "Wave 2 — Superseded by the Wave 1.2 baseline"):
- 2.A: after FRE-323 lands, `make eval-recovery RUN=consolidator-fix-verify --prompt memory_canary_recall` produces a Turn node in Neo4j matching turn 1's `trace_id`, and the canary's distinctive content (`ultramarine`, the UUID) appears as Entity nodes.
- 2.B: after FRE-324 lands, the same prompt's turn 2 response contains the word `ultramarine`.
- The previously planned `canary_infra.py`, `canary_memory.py`, `canary_cleanup.py` scripts and their Make targets are no longer in scope; the harness covers their detection role.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Geometry fix changes baseline behaviour so much that the original regression "disappears" | Acceptable. Document the change in the survey report so the lesson is not lost. The point of recovery is restoring capability, not preserving the broken baseline. |
| Survey assumes wrong ES index or event names | Survey script fails loudly on missing index; confirm field names by reading `telemetry/` and `captains_log/` first. |
| Memory canary pollutes Neo4j across re-runs | `:Canary` label + cleanup script + every canary uses a unique UUID. |
| Service not running when canaries execute | Canary scripts precondition-check `/health` before any work. |
| `/chat` doesn't surface `trace_id` in its response | One-line addition to `service/app.py`; do it as part of Wave 1 if needed and call it out in the wave's PR description. |
| Compression validator rejects an existing valid prod config in cloud env | Run the validator against `ENV=cloud` settings as part of Wave 0 verification. |
| Confusing this plan's scope with the source doc's full Wave 0–5 scope | The "Scope of this file" header makes scope explicit; the follow-up plan will be a separate file referenced from MASTER_PLAN. |

---

## Open items deferred to the follow-up plan

- Choice of skill-injection strategy (always-on manifest vs top-k vs model-requested).
- Loop-gate policy redesign (progressive friction, info-gain ledger, plan-observe-reflect).
- Concrete content of the `AGENT_RECOVERY_PROFILE` flag bundle.
- Permanent compression calibration per profile (this plan only fixes the obviously-broken default).
- Promotion of the recovery profile from flag bundle to brainstem `MODE=diagnostic`.
- Permanent telemetry instrumentation for "needed but missing" skills.
- **Labelled extraction-quality eval for `gpt-5.4-nano`** (recall@k for known entities, F1 against gold labels, comparison against `claude-haiku-4-5`, `qwen3-8b`, etc.). The recovery plan only verifies that extraction is *functional* and produces correct output for a single canary. Whether `gpt-5.4-nano` is the *right* model for entity_extraction / captains_log / insights — and whether sharing one model across all three roles is acceptable — is a separate research workstream.
- **Labelled embedding-quality eval for `qwen3-embedding-0.6b`** (retrieval recall@k against held-out queries, comparison vs alternatives). The recovery plan only verifies the embedding model is functional (non-zero, non-collapsed). Whether it produces semantically useful vectors is a separate research workstream.
- Decoupling extraction roles: today `entity_extraction_role`, `captains_log_role`, and `insights_role` all share `gpt-5.4-nano`. Splitting them is a follow-up question once quality data exists.
