# ADR-0063: Primitive Tools & Action-Boundary Governance

**Status**: Accepted — In Review
**Date**: 2026-04-24
**Deciders**: Project owner
**Depends on**: ADR-0005 (Governance Configuration), ADR-0028 (External Tool CLI Migration), ADR-0032 (Hybrid Tool Calling Strategy), ADR-0044 (Provider Abstraction), ADR-0062 (Tool Loop Gate)
**Supersedes in part**: FRE-252 TaskType tool-allowlist consumption in `evaluate_governance()`
**Related**: ADR-0053 (Gate Feedback Monitoring), ADR-0058 (Self-Improvement Pipeline), FRE-226 (Agent Self-Updating Skills)
**Linear Epic**: *(to be created — "Primitive Tool Capability + Tool-Filter Decoupling")*
**Migration Plan**: `docs/plans/2026-04-24-primitive-tools-migration-plan.md`

---

## Context

### The trigger

On 2026-04-23, FRE-254 shipped `task_type_policies` in `config/governance/tools.yaml`, wiring per-TaskType `allowed_categories` into `get_tool_definitions_for_llm()`. The design intent was sound — conversational intents drop the ~4,200-token tool payload, saving latency. The failure mode was not: the deterministic regex intent classifier defaults unmatched messages to `CONVERSATIONAL`, which intersects to an empty category list, which strips every tool from the request. The production deployment system prompt still named specific tools (`self_telemetry_query`, `infra_health`) and both Qwen3.6 (local) and Claude Sonnet (cloud) responded with Gemini-style `<tool_code>print(fn(args))</tool_code>` pseudo-code, which no parser recognized. Once one assistant turn emitted that format, subsequent turns in the same session mimicked it even when tools were properly passed.

Full forensic diagnosis and the four immediate fixes shipped in commit `1b35e02`.

### The larger question

The tactical fix restored correctness. It did not address the architecture that produced the failure class: **pre-flight gates stripping capability based on classifier output.**

Four distinct gates currently sit between the user message and the LLM:

1. Stage 3 — `evaluate_governance()` intersects task type × mode → `allowed_tool_categories`
2. `get_tool_definitions_for_llm(allowed_categories=…)` filters the tool registry
3. Per-TaskType `orchestrator_max_tool_iterations_by_task_type` caps turns
4. ADR-0062 loop gate blocks identical or consecutive tool calls

Each layer is individually reasonable. Collectively they produce a surface where a classifier miss silently strips the agent's capability to act. The bug was catastrophic in effect and invisible in the logs until a capture was forensically examined.

A secondary observation: the agent could not diagnose this bug on its own, because its 15 hardcoded tools could not inspect its own pipeline source code. A developer with `grep`, `curl`, and a scratch Python script diagnosed it in under an hour. The gap between "what the agent can do" and "what investigating its behavior requires" is itself a design signal.

### Research investment to preserve

This ADR explicitly preserves prior decisions that remain load-bearing:

- **ADR-0005 Governance Configuration** — mode-based policy, approval semantics, path allowlists. The governance *model* stays; only one consumer of that model changes.
- **ADR-0028 External Tool CLI Migration** — native > CLI > MCP ordering. This ADR extends that logic to its natural conclusion: a single `bash` primitive + skill docs replaces most Tier-1 native tools for free-form shell ops.
- **ADR-0032 Hybrid Tool Calling Strategy** — `ToolCallingStrategy.NATIVE` / `PROMPT_INJECTED` / `DISABLED` remain. New primitives are ordinary entries in the tool registry.
- **ADR-0044 Provider Abstraction** — LocalLLMClient / LiteLLMClient dispatch unchanged.
- **ADR-0062 Tool Loop Gate** — retained, signal severity split per §Decision D5.
- **FRE-254** intent classifier, decomposition assessment, memory retrieval, context assembly — all retained. Only the `allowed_tool_categories` wire into the tool loader is severed.

### North star vs. near-term path

The north star is Claude-Code-style capability: a small set of general primitives (`bash`, `read`, `write`, `run_python`) + skill documentation + action-boundary governance (user approval, sandbox policy), rather than a curated tool catalog. This ADR does not attempt to reach the north star in one pivot. It takes a phased path that retains reversibility and compounds learning.

The six phases in the migration plan are ordered so that every phase is independently shippable, independently reversible, and produces data that informs the next phase. If at phase 4 the evaluation gate shows Qwen3.6 cannot drive primitives as reliably as curated tools, the phase-6 deletion of legacy tools simply doesn't happen — deprecated tools stay flag-gated and available. Pivoting safely is worth more than pivoting fast.

---

## Decision

### D1 — Sever the TaskType → tool-filter wire

Remove `task_type_policies` from `config/governance/tools.yaml`. Remove the intersection logic in `evaluate_governance()` that produces `allowed_tool_categories` from classified intent. Remove the consumer in `step_llm_call()` that filters `get_tool_definitions_for_llm()` by that set.

The intent classifier continues to classify. Its output still drives:

- Per-TaskType iteration budgets (`orchestrator_max_tool_iterations_by_task_type`)
- Decomposition strategy (SINGLE / HYBRID / DECOMPOSE / DELEGATE)
- Memory retrieval style (MEMORY_RECALL vs task-assist)
- Context assembly depth
- Telemetry grouping

What it no longer drives: which tools the model sees. Every turn sees every tool the **mode** allows (`allowed_in_modes`). Mode enforcement remains.

Token cost: Anthropic `cache_control` on the tool list (already shipped in FRE-254) absorbs the cached hit at ~10% of write cost. For local Qwen, llama-server `cache_prompt: true` (already shipped) provides prefix KV reuse.

### D2 — Four new primitive tools

Add four tools to the registry. All sandboxed. All governed at action-boundary.

| Tool | Signature | Governance axis |
|---|---|---|
| `bash` | `bash(command: str, timeout_seconds: int = 30)` | First-word command allowlist per mode; high-risk commands trigger AG-UI approval |
| `read` | `read(path: str, max_bytes: int = 1_048_576)` | Path allowlist (`allowed_paths` / `forbidden_paths`); inherits existing `read_file` config |
| `write` | `write(path: str, content: str, mode: "overwrite" \| "append" = "overwrite")` | Path allowlist; anything outside scratch dir requires approval |
| `run_python` | `run_python(script: str, timeout_seconds: int = 60)` | Docker sandbox, no-network default, bind-mounted scratch dir only |

Sandbox design — deferred to the migration plan doc; summary: `run_python` runs in a short-lived container based on a minimal Python image, with `/sandbox` bind-mount (scratch), no network unless explicitly granted per call, read-only root filesystem, non-root UID, seccomp profile. `bash` runs in the service container (it needs access to `curl`, `docker`, `gh`, `psql`, etc.) but gated by command allowlist and approval; dangerous commands (`rm -rf`, `dd`, `mkfs`, `wget`, any command containing `sudo`) are hard-denied before reaching the shell.

### D3 — Action-boundary governance for the four primitives

A new AG-UI message type, `tool_approval_request`, is emitted when:

- `bash` receives a command not in the auto-approve allowlist for the current mode
- `write` targets a path outside the unattended scratch scope
- `run_python` requests network access

The PWA renders an approval prompt. The agent pauses until approval arrives or timeout (configurable, default 60s, timeout = deny). All approval decisions are written to `agent-captains-captures-*` for future policy learning.

Mode-aware defaults:

| Mode | `bash` | `read` | `write` | `run_python` |
|---|---|---|---|---|
| NORMAL | allowlist + approval | path-governed | scratch unattended / approval elsewhere | on |
| ALERT | read-only subset (`curl -XGET`, `grep`, `ls`) | path-governed | scratch only, no approval | on |
| DEGRADED | read-only subset | path-governed | scratch only, no approval | on |
| LOCKDOWN | disabled | essential paths only | disabled | disabled |
| RECOVERY | read-only subset | path-governed | scratch only, no approval | disabled |

### D4 — Deprecate curated tools superseded by primitives

Eight tools become redundant with `bash` + skill docs:

| Deprecated | Replacement |
|---|---|
| `read_file` | `read` |
| `list_directory` | `bash ls` |
| `system_metrics_snapshot` | `bash top -bn1` / `free -m` / `df -h` |
| `self_telemetry_query` | `bash curl http://elasticsearch:9200/... \| jq` via skill doc |
| `query_elasticsearch` | `bash curl http://elasticsearch:9200/... \| jq` via skill doc |
| `fetch_url` | `bash curl …` |
| `run_sysdiag` | `bash` (it is already a subprocess allowlist) |
| `infra_health` | `run_python` multi-step script via skill doc |

Six API-wrapper tools are **retained** because they carry credentials, auth flows, or schemas that do not cleanly unfold into `bash`:

- `web_search` (SearXNG with pagination)
- `perplexity_query` (Perplexity API key + schema)
- `search_memory` (Neo4j Cypher with KG scoring/freshness)
- `create_linear_issue`, `find_linear_issues`, `list_linear_projects` (GraphQL + Linear API key)
- `get_library_docs` (Context7 MCP contract)

Retention is a judgment call, not a technical constraint. Revisit after phase 4 ships.

### D5 — Split loop gate signal severity

ADR-0062 provides three signals. They are not equally load-bearing:

- **Output identity** (same args produce same output ≥2 times, non-`loop_output_sensitive`): genuinely pathological. Keep `BLOCK_OUTPUT` terminal.
- **Call identity** (same args called >N times): sometimes legitimate (retry after transient failure), sometimes a loop. Convert to advisory at `loop_max_per_signature` (inject hint to tool result), terminal at `loop_max_per_signature + 2`.
- **Consecutive count** (same tool N times in a row, different args): usually legitimate (reading N files, polling a status endpoint). Convert to advisory only; never terminal.

Advisory decisions inject a hint message into the tool result content ("You have called `X` N times consecutively — consider whether the result is stable") but allow execution.

### D6 — Fix the `model_config` role-key lookup bug

`executor.py:1296` looks up `model_configs.get(model_role.value)` (always `"primary"`). When the cloud profile redirects `primary → claude_sonnet`, this returns the Qwen config. Today both resolve to NATIVE so the symptom is dormant. Fix: resolve through `ExecutionProfile.primary_model` when a profile is active, matching the `get_llm_client()` resolution logic in `factory.py:97-100`.

### D7 — Skill docs become load-bearing (FRE-226 acceleration)

The model relies on tool signatures to know what a tool does. `bash` has no signature — it must be paired with skill documentation. FRE-226 (self-updating skills, agentskills.io format) moves from "Wave 4" to "inside the pivot" at phase 3. Each deprecated tool gets a skill doc before its removal.

FRE-226's original scope (agent writing skills for itself via self-improvement) is phase 2 of FRE-226; the pivot needs only phase 1 — hand-authored skill docs the agent reads.

---

## Consequences

### What gets easier

- **Capability composition.** The model can write a short Python script, run it, parse output, decide next step — the pattern that let a human developer diagnose FRE-254-class bugs in under an hour. No hardcoded tool for every shape of inspection.
- **New capability has zero tool-registry overhead.** Today: add a tool = Python executor + ToolDefinition + `tools.yaml` entry + unit tests + docstring. Tomorrow: add a skill doc describing a `bash` or `run_python` idiom. Iteration cost drops by an order of magnitude.
- **Self-introspection.** `bash grep -rn 'tool_defs' /app/src/` lets the agent read its own pipeline source when investigating a failure. Categorically new capability.
- **Fewer catastrophic failure classes.** The FRE-254 failure mode (classifier → empty allowlist → silent tool strip) cannot occur when there is no classifier → allowlist wire.

### What gets harder

- **Model preference risk.** The model must *choose* `bash curl …` over memory-derived tool names it was trained on. Qwen3.6-35-A3B is code-capable; Claude/GPT almost certainly handle this natively. The evaluation gate in phase 3 is the verification mechanism.
- **Skill docs become production-critical.** Today tool docstrings are nice-to-have. After the pivot, skill docs are the agent's manual. FRE-226 delivery is on the pivot's critical path.
- **Telemetry aggregation semantics shift.** Today: `tool_name=self_telemetry_query`. Tomorrow: `tool_name=bash, command_prefix=curl` with `logical_tool=query_elasticsearch` as a derived field. Dashboards and alerts need a one-time update.
- **Rate limiting per-command, not per-tool.** `tools.yaml` gets a `bash_commands:` section with per-prefix rate limits.
- **Sandbox operational burden.** A Docker-in-Docker path for `run_python`, container lifecycle, filesystem scope enforcement. This is new surface to maintain and monitor.

### What categorically does not change

- Gateway stages 1, 2, 4, 5, 6, 7 — unchanged.
- Intent classifier — unchanged.
- Decomposition strategy — unchanged.
- Memory retrieval — unchanged.
- Mode enforcement — unchanged.
- Expansion/delegation logic — unchanged.
- LLM client dispatch (Local vs LiteLLM) — unchanged.
- Existing 6 retained API-wrapper tools — unchanged.
- Cost budgets, weekly cloud budget — unchanged.
- Governance *model* (modes, approval semantics, path allowlists) — unchanged. Only the *mechanism* for tool governance moves from pre-flight filter to action-boundary check.

---

## Alternatives Considered

### A. Minimal fix — revert FRE-252's tool filter, ship nothing else

*Rejected.* Eliminates the bug but leaves the agent without capability-compositor tools. Does not address the underlying asymmetry between "what the agent can do" and "what diagnosing the agent requires." Misses the opportunity exposed by the bug.

### B. Full Claude-Code replica — 4 primitives, no pre-LLM gateway, no intent classifier, no modes

*Rejected for now, held as north star.* The agent's multi-session memory, mode-aware operation, cloud/local profile routing, and sub-agent expansion all require pre-LLM logic that a stateless CLI does not. A full replica would discard research investment without gaining corresponding value. The 6-phase migration plan leaves this option open — if phases 1-5 demonstrate that curated tool wrappers add no value over primitives + skills, phase 6 deletes them; if they do add value, they stay.

### C. Additive primitives with no deprecation — keep all 15 tools, add 4 more

*Rejected.* Ships capability without forcing the skill-doc discipline that makes primitives usable. Keeps the existing TaskType→tool-filter failure class intact. Accumulates maintenance cost without reducing surface area. Half the pivot, all the complexity.

### D. Replace curated tools with MCP-wrapped primitives

*Rejected.* ADR-0028 established that MCP adds ~200-1,400 tokens per tool vs near-zero for CLI. MCP-wrapping `bash` adds overhead for a capability that works natively via the OpenAI tools array.

### E. Per-call LLM planner that selects which tools to expose

*Rejected for the current pivot, revisit post-P5.* A cheap pre-call LLM that reads the user message and returns a tool shortlist is a plausible replacement for the regex classifier's filter role. It has better recall than regex and fails gracefully (if planner picks no tools, show all tools is a safe default). Cost: one extra LLM call per turn. Worth evaluating after primitives are in production.

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Qwen3.6 cannot drive `bash`/`run_python` as reliably as curated tools | Medium | Phase 3 evaluation gate: 20 representative traces, primitives must meet-or-exceed curated-tool success rate before any deprecation |
| Sandbox escape / privilege escalation via `run_python` | Medium | Docker container with read-only root, non-root UID, seccomp profile, no-network default, short timeout. Pentest before phase 2 ship. |
| Approval fatigue (too many prompts) | Medium | Mode-aware allowlist tuned from telemetry after phase 2. Auto-approve patterns for repeated safe commands (per-session). |
| Telemetry dashboard breakage | Low | Shim emits `logical_tool` derived from command prefix for backward-compatible aggregation. One-time Kibana visualization update. |
| Token cost regression from always-on tool list | Low | Anthropic cache_control + llama-server cache_prompt already deployed. Measure in phase 1; if regression >10% of baseline, investigate. |
| Loop gate regression from advisory signals | Low | Phase 5 keeps output-identity terminal (the strongest signal). Advisory signals carry telemetry; if model ignores advisory hints pathologically, convert back to terminal per-tool. |
| FRE-226 slippage blocks phase 3 | Medium | Hand-authored skill docs acceptable for phase 3; self-updating is FRE-226 phase 2 (post-pivot). |
| Broken reversibility — can't rollback if something regresses | Low | Deprecated tools stay implemented behind `AGENT_LEGACY_TOOLS_ENABLED` feature flag for ≥2 weeks after phase 4. Code deletion is phase 6 only after sustained stability. |

---

## Implementation Plan

See `docs/plans/2026-04-24-primitive-tools-migration-plan.md` for the full phased migration, per-phase success gates, reversibility mechanisms, and Wave 2 coexistence strategy.

Summary:

| Phase | Scope | Risk | Gate to next |
|---|---|---|---|
| P1 | Sever TaskType→tool-filter wire | Low | Token cost / latency within 5% of baseline after 48h |
| P2 | Add `bash` / `read` / `write` / `run_python` + sandbox + approval | Medium | Pentest clean; approval UX acceptable |
| P3 | Skill docs for 8 deprecated tools + model eval | Medium | Primitive success ≥ curated success on 20 traces |
| P4 | Flag-gated deprecation (`AGENT_LEGACY_TOOLS_ENABLED=false`) | Medium | 2 weeks production stability |
| P5 | Loop gate signal split + `model_config` fix | Low | Existing output-identity decisions unchanged |
| P6 | Delete legacy tool code | Low | `AGENT_LEGACY_TOOLS_ENABLED` untouched ≥2 weeks |

---

## Open Questions

1. **Approval policy learning.** The AG-UI approval capture data (D3) could train a policy that auto-approves patterns the user has approved N times. Is this in scope for phase 2, or a phase 7 item? *Current stance: out of scope for pivot; track as future ADR.*
2. **Remote sandbox for `run_python`.** Should the sandbox run on the same VPS or a separate constrained host? *Current stance: same host for phase 2 (Docker-in-Docker via DIND socket), evaluate isolation upgrade if threat model warrants.*
3. **Per-session auto-approval memory.** Does approval persist across turns in a session, or re-prompt on every occurrence? *Current stance: per-session for exact-command matches, prompt for novel commands.*
4. **API wrapper retention.** Six wrappers retained in D4. Should they become skill docs in a phase 7? *Current stance: decide after phase 4 data.*
5. **Claude Code parity for governance signals.** Claude Code uses `settings.json` allow/deny patterns. Should we adopt the same format for tool governance to enable policy portability? *Current stance: evaluate post-P4.*

---

## Success Metrics

- **Recurrence of FRE-254-class bugs**: zero in 60 days post-phase-1. (Classifier → empty-tool-list path no longer exists.)
- **Primitive-to-curated success rate ratio** (phase 3): ≥1.0 on the 20-trace eval.
- **Token cost regression** (phase 1): prompt cache hit rate maintains ≥70%; uncached prefill cost within 5% of pre-pivot baseline.
- **Time-to-diagnose a synthetic tool-pipeline failure** (post-phase-2): agent can introspect its own pipeline source via `bash grep` and identify the faulty module in <5 turns.
- **New-tool iteration cost** (post-phase-4): adding a new capability takes ≤1 skill doc instead of ≥5 files (executor + definition + registry entry + yaml + tests).

---

## References

- Forensic diagnosis of FRE-254 capability-strip bug: conversation 2026-04-24 (`/tmp/tool-bug-investigation/` analysis artifacts; fix commit `1b35e02`)
- Claude Code architectural pattern observation — conversation 2026-04-24 (self-reflection on diagnostic methodology)
- ADR-0028 §Industry Standardization — CLI-first evidence from OpenClaw, mcp2cli, Phil Rentier's analysis
- ADR-0062 — per-tool FSM loop detection (retained, severity split per D5)
- ADR-0054 — feedback stream bus convention (no interaction with this ADR)
