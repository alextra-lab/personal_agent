# ADR-0068: Agent Self-Telemetry Data Plane and Query Interface

**Status**: Accepted
**Date**: 2026-05-10
**Deciders**: Project owner
**Related**: ADR-0028 (CLI-first tool tiers — Tier 1/2/3 ordering), ADR-0063 (primitive tools — created bash/read/write/run_python), ADR-0066 (skill routing — skill docs as schema), ADR-0030 (Captain's Log scope)
**Linear**: FRE-258 (this ADR), FRE-265 (legacy tool deletion, gate ≥ 2026-05-12)

---

## Context

### The triggering failure

On 2026-04-23 the agent was asked: *"Check token stats for the Local path — prompt cache, token usage, compaction, and anything other token metrics proving insight. Analyze by model and UI path."* It entered a 24-call loop on `self_telemetry_query` (local path) and a 50-call loop on `query_elasticsearch` (cloud path), both terminating via the loop gate at iteration limit. The user received raw tool-call JSON instead of an answer.

FRE-258 was filed with the original diagnosis: *"the agent had no way to answer the question because the telemetry query infrastructure isn't designed for it."* The recommended fix was a new schema-aware self-telemetry tool — blocked on FRE-261 (PIVOT-2 primitives).

### What changed before this ADR was written

- **FRE-261 shipped 2026-04-27** (`bash`, `read`, `write`, `run_python` primitives live in code, gated by `AGENT_PRIMITIVE_TOOLS_ENABLED`).
- **`self_telemetry_query` and `query_elasticsearch` are gated off** — `AGENT_LEGACY_TOOLS_ENABLED=False` is production default.
- **Skill docs** (`docs/skills/query-elasticsearch.md`, `seshat-observations.md`, `system-metrics.md`) already document the primitives + ES path the agent should follow.
- **FRE-265 (legacy tool deletion)** gate opens 2026-05-12; both tools will be removed from HEAD shortly after.
- **ADR-0066 (skill routing)** established skill docs as the canonical schema surface for `bash` + `read_skill` workflows — the same pattern applies here.

A read-only audit (3 parallel Explore agents, 2026-05-10) confirmed that the original architectural diagnosis was partially wrong, and the residual problems are smaller and more concrete than originally framed.

### Key audit findings

**The "JSONL is missing fields ES has" framing is incorrect.** The structlog → JSONL → ES pipeline (`telemetry/logger.py:126-262`, `es_handler.py:78-179`) sends identical payloads to both sinks for every `log.info("event", **kwargs)` call. Asymmetry is per-emit-site, not per-sink.

**Real emit-site gaps:**

| Emit site | Missing / wrong |
|---|---|
| `llm_client/litellm_client.py:403-415` (cloud `litellm_request_complete`) | `completion_tokens` computed at line 340 but not logged; uses `elapsed_s` (seconds) not `latency_ms`; lacks `endpoint`, `provider`, `api_type`, `span_id`, `total_tokens`; renames `cache_creation_input_tokens` → `cache_write_tokens` |
| `orchestrator/executor.py:1771-1778` (step-level `model_call_completed`) | Only `tokens` (total), `model_role`, `duration_ms` — same event name as client-level emit, different shape. Confusing for any consumer. |

The local `llm_client/client.py:454-468` emit is rich and serves as the reference shape.

**ES-only documents (legitimately not in JSONL):** `request_trace`, `request_trace_step`, `request_latency_breakdown`, `request_latency_phase` — written via direct ES API in `telemetry/es_logger.py:230-469`, never through structlog.

**ES template / emit name mismatch:** `docker/elasticsearch/index-template.json` declares explicit field mappings for `input_tokens`, `output_tokens`, `model_name`, `tokens_used` — but emits write `prompt_tokens`, `completion_tokens`, `model` / `model_id`. `dynamic:true` covers runtime indexing but the explicit mapping is dead code and auto-typed fields are uncoordinated.

**No internal Python callers of `self_telemetry_query_executor` outside the LLM tool path.** The comment block at `self_telemetry.py:36-69` ("internal use by orchestrator, brainstem, Captain's Log") is aspirational documentation. `brainstem/`, `captains_log/`, and `second_brain/` consult lower-level helpers (`metrics.get_trace_events`, `read_captures`) directly. Zero migration shim is required when the tool is deleted.

**Live skill-doc inconsistency:** `docs/skills/query-elasticsearch.md:200-224` instructs the agent to use `run_python` to `from personal_agent.telemetry.metrics import query_events`. But `run_python` runs in the `seshat-sandbox-python:0.1` container where project source is not importable (`docs/skills/run-python.md:17`: *"App source is NOT importable. `from personal_agent import ...` will ImportError."*). This is a live bug in agent guidance.

**`read` primitive cannot access `current.jsonl` as a file:** `max_file_size_mb: 10` (`tools.yaml:86`) rejects the 19 MB live log. Workaround: `bash cat`/`tail`/`grep | jq` (auto-approved commands). The `read` primitive's default cap is sized for source files, not growing telemetry logs.

---

## Decision

### D1 — Primitives + skill docs are the canonical agent self-telemetry interface

No new self-telemetry tool will be built. The canonical interface for all agent introspection work is:

1. **bash** (via `curl http://elasticsearch:9200/_query` for ES) — primary path for complex aggregations, event-type queries, and request-trace lookups. Skill doc discipline in `query-elasticsearch.md` provides the index-name schema, known-bad patterns, and worked ES|QL examples.
2. **bash** (via `cat`/`tail`/`grep`/`jq /app/telemetry/logs/current.jsonl`) — for recent-event pattern work, trace-following, and local-path-only metrics that don't reach ES. Viable as long as the JSONL file remains bash-accessible and within the 50 KiB output cap (use `tail -n N` or `grep | head`).
3. **read** — appropriate only for small files (< 10 MB). Not usable on `current.jsonl` today; potentially usable on rotated backups (`.1`–`.5`) if those are smaller.

This matches the trajectory established by ADR-0063 (primitives) and ADR-0066 (skill docs as schema). It does not require any new tool registration, governance entry, or LLM-facing schema.

Schema discipline shifts from executable tool code to skill-doc tables. The precedent from `query-elasticsearch.md:91-118` — a hand-curated field map with known-bad patterns at `:43-67` — is the model. The audit surfaced that this doc needs updates (see D4).

Loop-prevention shifts from per-tool `loop_max_per_signature` to general primitive-level governance (`loop_max_per_signature: 3` for bash, `loop_consecutive_terminal: true` for bash at ALERT). Already in place per `tools.yaml:445-521`.

### D2 — `self_telemetry_query` and `query_elasticsearch` retire via FRE-265 with no replacement

FRE-265 (gate ≥ 2026-05-12) will delete both tool modules, their registration entries in `tools/__init__.py`, their governance entries in `tools.yaml`, and `AGENT_LEGACY_TOOLS_ENABLED` along with all branches keyed on it. This ADR confirms: **no replacement tool is needed**. The primitives + skill docs already cover the same surface.

No internal callers to migrate. No migration shim required.

### D3 — Cloud emit field parity (follow-up ticket)

The cloud `litellm_request_complete` emit at `llm_client/litellm_client.py:403-415` must be brought to parity with the local reference shape. Missing fields that the original FRE-258 trigger query requires:

- `completion_tokens` (computed at line 340, not logged)
- `latency_ms` (currently `elapsed_s` in seconds)
- `endpoint` / `provider` / `api_type` (implicit in `model` slug only)
- `total_tokens` (aliased to `tokens` — standardise)

Double-write deprecated name + new name during a one-release transition if any downstream ES query depends on `elapsed_s`. This is a Tier-3:Haiku mechanical fix. Filed as [FRE-351](https://linear.app/frenchforest/issue/FRE-351) (Needs Approval).

The step-level `model_call_completed` emit at `orchestrator/executor.py:1771-1778` reuses the same event name as the client-level emit with a much sparser payload. Options: rename the step-level event (e.g. `llm_step_completed`) or align its fields with the client-level shape. Either way it should not silently shadow the richer event for a consumer doing event-type queries. Filed as [FRE-352](https://linear.app/frenchforest/issue/FRE-352) (Needs Approval).

### D4 — ES index template field names (follow-up ticket)

`docker/elasticsearch/index-template.json` explicit field mappings (`input_tokens`, `output_tokens`, `model_name`, `tokens_used`) do not match actual emit field names (`prompt_tokens`, `completion_tokens`, `model` / `model_id`, `tokens`). With `dynamic:true` this is silent and functional today, but the mismatch will surface when someone writes an explicit aggregation expecting the declared types.

After D3 ships (so the mapping reflects the corrected emit shape), reconcile the template: delete dead declarations and add explicit mappings for the fields that are actually written. Filed as [FRE-353](https://linear.app/frenchforest/issue/FRE-353) (Needs Approval).

### D5 — Fix skill-doc run_python inconsistency (follow-up ticket)

`docs/skills/query-elasticsearch.md:200-224` must be rewritten. The current snippet (`from personal_agent.telemetry.metrics import query_events`) will ImportError in the sandbox container. Replace with a `bash curl` snippet against the ES `_query` endpoint that achieves the same result (recent captures from `agent-captains-captures-*`, recent reflections from `agent-captains-reflections-*`). Filed as [FRE-354](https://linear.app/frenchforest/issue/FRE-354) (Needs Approval, Tier-3:Haiku — mechanical doc update).

### D6 — `read` primitive log-file accessibility (follow-up ticket, decision deferred)

The 10 MB `max_file_size_mb` cap on the `read` primitive (`tools.yaml:86`) blocks access to `current.jsonl` (19 MB). Three options exist, each with different consequences for the tool's safety profile:

- **Accept bash-only**: close as wontfix. Agent uses `bash tail` / `bash grep` for log inspection. Simple; consistent with the skill docs' current primary path.
- **Bump cap**: raises the bar for accidental memory exhaustion for all `read` callers; the cap was sized for source files.
- **Add `mode: tail/head/range` parameter**: adds complexity but solves the root cause cleanly.

This decision warrants user input. Filed as [FRE-355](https://linear.app/frenchforest/issue/FRE-355) (Needs Approval, Tier-2:Sonnet — design question + implementation).

### D7 — Write `docs/skills/self-telemetry.md` (follow-up ticket)

No unified skill doc currently covers agent introspection. `seshat-observations.md` covers dashboards + the `/observations/` API. `query-elasticsearch.md` covers generic ES queries. `system-metrics.md` covers CPU/mem. None cover the specific workflow of *"answer a question about my own token usage, caching behaviour, or latency profile."*

A `self-telemetry.md` skill doc should consolidate: which indices to query for which question type, how to separate Local vs Cloud path events, how to include Captain's Log captures in a holistic answer, and a worked end-to-end example reproducing the FRE-258 trigger query. Filed as [FRE-356](https://linear.app/frenchforest/issue/FRE-356) (Needs Approval, Tier-3:Haiku — doc authoring).

---

## Consequences

### Immediate (this ADR)

- Agent self-telemetry follows the skill-doc path: `bash curl` to ES, `bash tail/grep/jq` to JSONL. No new tool needed.
- FRE-265 proceeds with no replacement blocker.
- The "internal callers" framing in `self_telemetry.py:36-69` is confirmed false; no migration shim is produced.

### Follow-ups (each a separate Linear ticket with its own approval gate)

| Ticket | Tier | Seq |
|---|---|---|
| [FRE-351](https://linear.app/frenchforest/issue/FRE-351) Cloud emit field parity — `litellm_request_complete` add `completion_tokens`, `endpoint`, `provider`, rename `elapsed_s` → `latency_ms` | Tier-3:Haiku | Ship before D4 |
| [FRE-352](https://linear.app/frenchforest/issue/FRE-352) Step-level `model_call_completed` disambiguation — rename or align | Tier-2:Sonnet | Ship with D3 |
| [FRE-353](https://linear.app/frenchforest/issue/FRE-353) ES `agent-logs-*` index template reconciliation | Tier-3:Haiku | After D3 ships |
| [FRE-354](https://linear.app/frenchforest/issue/FRE-354) Fix `query-elasticsearch.md` skill doc — rewrite run_python snippet as bash curl | Tier-3:Haiku | Unblocked immediately |
| [FRE-355](https://linear.app/frenchforest/issue/FRE-355) `read` primitive log-file tailing strategy | Tier-2:Sonnet | Decision wanted first |
| [FRE-356](https://linear.app/frenchforest/issue/FRE-356) Write `docs/skills/self-telemetry.md` | Tier-3:Haiku | After FRE-354 is in |

### Deferred

**ADR-0030 amendment**: ADR-0030 scoped Captain's Log as an observation-only surface (write side only). ADR-0067 (FRE-348) already amended that in practice by making reflections re-injectable into context. FRE-349 (G3 — insights surfacing) will further extend it. Once FRE-349 lands, ADR-0030 should receive an explicit in-place amendment noting that the Captain's Log is now both write-side and read-side for the agent. Out of scope for this ADR.

---

## Alternatives considered

### Schema-aware ES query builder

A Python layer that introspects `_mapping` and builds typed ES|QL from high-level intent (e.g. `get_metric(name="prompt_tokens", labels={"model": "qwen3"}, window="1h")`). Rejected: single-purpose, high build cost, conflicts with the direction established by ADR-0063 (primitives + skill docs replace curated tools, not add new ones). The query schema in skill docs achieves the same effect with zero new code.

### Pre-aggregated metrics API

A `/metrics` HTTP endpoint that computes aggregate answers on demand. Rejected for the same reason: only the agent queries this; Kibana already serves human consumers. Adding an API for one consumer is overhead the architecture doesn't need.

### JSONL-only (deprecate ES for self-queries)

Would simplify the data plane but loses the ES-only structured documents (`request_trace_step`, `request_latency_breakdown`) and the ILM/retention machinery already in place. Rejected.

### Keep `self_telemetry_query` (extend, don't delete)

Extending the canned query types with a `tokens` type covering model × path would directly solve the FRE-258 trigger query, but: the tool is already on the deletion path (FRE-265), the 7 existing query types overlap substantially with what skill docs + bash can do, and every extension embeds schema assumptions that will drift as the telemetry evolves. Rejected — primitives + skill docs provide the same capability without a maintenance surface.
