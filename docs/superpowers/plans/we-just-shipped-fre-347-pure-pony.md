# FRE-258 — Adaptive agent self-telemetry (ADR + cleanups)

> **Linear**: [FRE-258](https://linear.app/frenchforest/issue/FRE-258) — Tier-1:Opus, Approved
> **Plan author**: Claude (Opus, plan mode)
> **Plan date**: 2026-05-10
> **Branch strategy**: docs-only → push direct to main (per memory). Code follow-ups (filed as separate Linear tickets) get their own branch + PR.

---

## Context

FRE-258 was filed 2026-04-23 after a live session where the agent burned 24 calls on `self_telemetry_query` and 50 calls on `query_elasticsearch` answering *"check token stats for the Local path — prompt cache, token usage, compaction, and anything other token metrics."* The original framing assumed the fix was **a new schema-aware self-telemetry tool**, blocked on FRE-261 (PIVOT-2 primitives).

The world has moved since:

- **FRE-261 shipped 2026-04-27** — `bash`, `read`, `write`, `run_python` primitives are in code (gated off via `AGENT_PRIMITIVE_TOOLS_ENABLED=False` pending production rollout).
- **`self_telemetry_query` and `query_elasticsearch` are gated off** — `AGENT_LEGACY_TOOLS_ENABLED=False` is the production default.
- **FRE-265 (legacy tool deletion) gate opens 2026-05-12** — both tools will be removed from HEAD shortly after.
- **Skill docs** (`docs/skills/query-elasticsearch.md`, `seshat-observations.md`, `system-metrics.md`) already document the primitives+ES path the agent should follow.

The exploration (3 parallel Explore agents on 2026-05-10, summarised below) confirms the architectural questions in the original ticket are largely already answered by the deprecation plan. The unresolved problems aren't architectural — they're **smaller, concrete gaps that prevent primitives+skills from being a clean replacement.**

This plan writes ADR-0068 to ratify the architecture and files focused follow-up tickets for the discovered gaps. It does **not** implement code — code follow-ups become separate Linear issues with their own approval gates.

---

## Audit findings (the input to ADR-0068)

### Tool inventory

- `src/personal_agent/tools/self_telemetry.py` (984 lines): 7 canned query types (`health`, `errors`, `performance`, `interactions`, `trace`, `latency`, `events`). Reads from `telemetry/logs/current.jsonl` (+ rotated backups) and Captain's Log captures. Registered only when `legacy_tools_enabled=True` (`tools/__init__.py:138`); governance entry `config/governance/tools.yaml:541-548`.
- `src/personal_agent/tools/elasticsearch.py:22-81`: 4 sub-actions (`esql`, `list_indices`, `get_mappings`, `get_shards`). Zero schema awareness — the description never names indices or fields. Same legacy gate; governance `tools.yaml:560-569` (`loop_max_per_signature: 3`, `loop_consecutive_terminal: true` from FRE-279).
- **No internal Python callers** of `self_telemetry_query_executor` outside the LLM tool path. `brainstem/`, `captains_log/`, `second_brain/` all consult lower-level helpers (`metrics.get_trace_events`, `read_captures`) directly — not the tool. The "internal-use patterns" comment block in `self_telemetry.py:36-69` is aspirational.

### FRE-261 primitives

- `bash` (`tools/primitives/bash.py:68-104`): allow-list approval (`cat`, `tail`, `grep`, `jq`, `curl`, `head` are auto-approved per `tools.yaml:445-510`); 50 KiB output cap with overflow file under `/tmp/agent_scratch/<trace_id>/`; hard-deny regex blocks `rm -rf`, `sudo`, `wget`, `ssh`. Runs **in the gateway container**, so paths are `/app/telemetry/...` not `/opt/seshat/...`.
- `read` (`tools/primitives/read.py:27-58`): `allowed_paths` includes `/app/**` and `/opt/seshat/**`, **but `max_file_size_mb: 10`** (`tools.yaml:86`) **rejects `current.jsonl` (19 MB)** with `too_large`. Workaround: pass `max_bytes` < 10 MB — but that returns the file head; newest events live at the tail.
- `run_python` (`tools/primitives/run_python.py`, `sandbox.py:91-241`): runs in `seshat-sandbox-python:0.1` Docker image, non-root, `--read-only`, `--cap-drop=ALL`, `--network=none` default, 64 MB tmpfs `/tmp`. Pre-installed: `requests httpx pandas numpy pyyaml psutil`. **Cannot import `personal_agent.*`** — `docs/skills/run-python.md:17` is explicit.

### Telemetry data plane

- The structlog → JSONL → ES pipeline (`telemetry/logger.py:126-262`, `telemetry/es_handler.py:78-179`) sends **identical payloads to both sinks** for any `log.info("event", **kwargs)` call. The FRE-258 framing of "JSONL is missing fields ES has" is **incorrect** — asymmetry exists per-emit-site, not per-sink.
- **Real gaps** (per-emit-site):
  - `llm_client/litellm_client.py:403-415` (cloud `litellm_request_complete`): `completion_tokens` is computed at line 340 but **not logged**. Uses `elapsed_s` (seconds) instead of `latency_ms`. Lacks `endpoint`, `provider`, `api_type`, `span_id`, `total_tokens`. Renames `cache_creation_input_tokens` → `cache_write_tokens`.
  - `llm_client/client.py:454-468` (local `model_call_completed`): rich — `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_read_tokens`, `latency_ms`, `model_id`, `endpoint`, `api_type`, `fallback_used`. **Reference shape.**
  - `orchestrator/executor.py:1771-1778` (step-level `model_call_completed`): only `tokens` (total), `model_role`, `duration_ms`. Same event name, different shape — confusing for any consumer.
- ES-only (bypass structlog): `request_trace`, `request_trace_step`, `request_latency_breakdown`, `request_latency_phase` written via direct ES API in `telemetry/es_logger.py:230-469`. Legitimately not in JSONL.
- **ES template / emit name mismatch**: `docker/elasticsearch/index-template.json` declares explicit mappings for `input_tokens`, `output_tokens`, `model_name`, `tokens_used` — but emits write `prompt_tokens`, `completion_tokens`, `model` / `model_id`. `dynamic:true` covers it, but the explicit mapping is dead code and the auto-typed fields aren't coordinated.
- JSONL retention: `RotatingFileHandler` 100 MB × 6 backups (~600 MB cap, no time-based roll). Live `current.jsonl` is 19.5 MB; rotation hasn't fired.

### Skill-doc inconsistency

`docs/skills/query-elasticsearch.md:200-224` instructs the agent to use `run_python` to import `personal_agent.telemetry.metrics.query_events` and `personal_agent.captains_log.capture.read_captures`. But `run-python.md:17` says: *"App source is NOT importable. `from personal_agent import ...` will ImportError."* The advised path doesn't work in the sandbox image. This is a live bug in agent guidance.

---

## Decision space — what ADR-0068 must decide

1. **Data plane (canonical store for agent self-queries)**:
   - Option A: JSONL-first + ES as audit/dashboard sink — aligned with current rich local emits.
   - Option B: ES-first via `bash curl` — aligned with current skill doc primary path; needs `legacy_tools_enabled` flag and lifecycle ILM policy already in place.
   - Option C: Hybrid — bash+read for JSONL, bash+curl for ES; agent picks per-question. *Status quo modulo cleanups; matches reality of current skill docs.*
2. **Query plane**:
   - Option A: Extend canned queries (add `tokens` query type to `self_telemetry_query`). Rejected — tool is being deleted.
   - Option B: Schema-aware ES query builder. Heavy build, single-purpose, conflicts with deprecation direction.
   - Option C: Pre-aggregated metrics API. Heavy build; only worth it if metrics are read in many places (they're not — humans use Kibana, agent uses ad-hoc queries).
   - Option D: **Primitives only** + skill docs as the schema. Aligned with FRE-261/FRE-265/FRE-263; lowest net new code.
3. **Field parity**: Cloud emit must reach reference shape (local `model_call_completed`). Executor step emit needs disambiguation (rename or align fields).
4. **ES index template**: reconcile declared field names with emit payloads, or delete dead-mapping declarations and accept dynamic typing with explicit conventions in skill docs.
5. **Skill-doc layout**: keep `query-elasticsearch.md` as primary and fix the run_python bug, or split off a new `self-telemetry.md` that consolidates the introspection workflow (JSONL via bash, ES via curl, Captain's Log via either, with worked examples for the original FRE-258 trigger query).

---

## Recommended approach

**ADR-0068 chooses C/D/skill-doc-consolidation:** primitives + skills are the canonical path; deprecate legacy tools per FRE-265 with no replacement; fix per-emit-site sparseness; reconcile the ES template; consolidate self-telemetry skill guidance.

This is the *least-additional-code* path and aligns with two existing ADRs (0063 primitive tools, 0066 skill routing). The ADR is small — its job is to ratify the trajectory and concretely scope the gaps the audit surfaced.

---

## Concrete deliverables

### 1. ADR-0068 (the primary deliverable)

**Path**: `docs/architecture_decisions/ADR-0068-agent-self-telemetry-data-plane.md`

**Sections**:
- Context (mirror this plan's context section, condensed)
- Decision: ratify primitives+skills as canonical; legacy tools retire via FRE-265 with no replacement
- Consequences:
  - Agent introspection workflow becomes: skill doc → bash/read/curl primitives → JSONL or ES
  - Schema discipline shifts from tool code to skill-doc tables (similar to `query-elasticsearch.md:91-118` curated mapping table)
  - Loop-prevention shifts from per-tool `loop_max_per_signature` to general primitive-level governance (already in place)
- Alternatives considered (briefly): schema-aware query builder, metrics API, JSONL-only
- Open questions (deferred to follow-ups, with Linear ticket IDs once filed)
- Migration: FRE-265 already removes the tools; ADR explicitly notes the brainstem/CL "internal callers" claim is unwired and no migration shim is needed

### 2. Linear follow-up tickets (filed Needs Approval, label `PersonalAgent`)

| Ticket title | Tier | Scope |
|---|---|---|
| **Cloud emit field parity** — `litellm_request_complete` add `completion_tokens`, `endpoint`, `provider`; rename `elapsed_s` → `latency_ms` and double-write through deprecation period | Tier-3:Haiku | `llm_client/litellm_client.py:403-415`. Mechanical. Tests: assert payload keys present. |
| **Executor step `model_call_completed` disambiguation** — either align with client-level shape or rename event | Tier-2:Sonnet | `orchestrator/executor.py:1771-1778`. Event-name collision is footgun for any future ES consumer. Coordinate with field-parity ticket. |
| **Reconcile ES `agent-logs-*` index template field names** — delete dead `input_tokens`/`output_tokens`/`model_name` mappings (or remap to actual emits) | Tier-3:Haiku | `docker/elasticsearch/index-template.json`. After field parity ticket so the new mapping reflects real fields. |
| **Fix `query-elasticsearch.md` skill doc** — `run_python` cannot import project modules | Tier-3:Haiku | `docs/skills/query-elasticsearch.md:200-224`. Either rewrite snippet to use `bash curl` against ES, or move project-import workflow to a tool that runs in the gateway (none currently exists). |
| **`read` primitive log-file accessibility** — decide max_bytes/tailing strategy for files >10 MB | Tier-2:Sonnet | `tools/primitives/read.py:27-58`, `config/governance/tools.yaml:86`. Options: (a) accept bash-only path for big logs (close as wontfix), (b) bump cap, (c) add `mode: tail/head/range` parameter. Decision wants user input. |
| *(optional)* **Write `docs/skills/self-telemetry.md`** — consolidate introspection workflow with worked example for the FRE-258 trigger query | Tier-3:Haiku | New file. Worked example: "show prompt-cache hits and miss counts by model for the last 2 hours, separated by Local vs Cloud path." |

### 3. MASTER_PLAN.md update

- Move FRE-258 to **Recently Completed** with a 2-3 line summary referencing ADR-0068 and the filed follow-ups
- Bump `Last updated` line at top
- If new tickets land in `Needs Approval`, add to that section

### 4. ADR-0030 footnote (defer — not in this plan)

The exploration noted ADR-0030 currently scopes Captain's Log as observation-only. ADR-0067 (FRE-348) already amended that direction by making reflections re-injectable. Once FRE-349 (G3 insights surfacing) lands, ADR-0030 deserves a single amendment block. Out of scope for this plan; flagging for later.

---

## Critical files (reference for ADR drafting)

**Tools / governance**:
- `src/personal_agent/tools/self_telemetry.py` (full)
- `src/personal_agent/tools/elasticsearch.py:22-81`
- `src/personal_agent/tools/__init__.py:54-180` (registration gates)
- `src/personal_agent/tools/primitives/{bash,read,write,run_python}.py`
- `src/personal_agent/tools/primitives/sandbox.py:91-241`
- `config/governance/tools.yaml:78-180, 424-521, 541-548, 560-569`

**Telemetry data plane**:
- `src/personal_agent/llm_client/client.py:454-468` (reference local emit shape)
- `src/personal_agent/llm_client/litellm_client.py:403-415` (sparse cloud emit)
- `src/personal_agent/orchestrator/executor.py:1771-1778` (sparse step emit)
- `src/personal_agent/captains_log/capture.py:40-65, 89-133` (TaskCapture write path)
- `src/personal_agent/telemetry/logger.py:126-262` (sink config)
- `src/personal_agent/telemetry/es_handler.py:78-179`
- `src/personal_agent/telemetry/es_logger.py:230-469` (direct ES writes)
- `src/personal_agent/telemetry/metrics.py:44-55, 137-170` (JSONL reader + rotation awareness)
- `docker/elasticsearch/index-template.json`

**Skill docs (the new "schema")**:
- `docs/skills/query-elasticsearch.md` (primary, has live bug at :200-224)
- `docs/skills/seshat-observations.md`
- `docs/skills/system-metrics.md`
- `docs/skills/run-python.md` (explicit non-importability rule at :17)
- `docs/skills/bash.md`, `read-write.md`

**Adjacent ADRs**:
- `docs/architecture_decisions/ADR-0063-primitive-tools-action-boundary-governance.md`
- `docs/architecture_decisions/ADR-0066-skill-routing-defaults-threshold-feedback-loop.md`
- `docs/architecture_decisions/ADR-0030-*` (Captain's Log scope — read for cross-reference, no amendment in this plan)

---

## Verification

- ADR-0068 is reviewable on iPad via GitHub (single doc on `main`)
- Each follow-up Linear ticket has reproducible evidence (file:line citations, audit findings cross-linked)
- `MASTER_PLAN.md` reflects FRE-258 done in Recently Completed table
- No code changes in this plan's scope — verification is doc-quality + linkage, not test runs

---

## Out of scope (explicitly)

- Implementing any of the cleanup tickets (each has its own approval cycle)
- ADR-0030 amendment (waits for FRE-349 to land)
- Designing a new metrics-aggregation API or schema-aware query builder (rejected by ADR-0068)
- Deleting `self_telemetry.py` / `elasticsearch.py` (FRE-265's job, calendar-gated to 2026-05-12)
- Changing `legacy_tools_enabled` or `primitive_tools_enabled` defaults (governance change, not architecture)
