# ADR-0086 — HYBRID/DECOMPOSE Routing for High-Complexity Artifact Builds (Unpin `TOOL_USE` Complexity + Tool-Using Discovery Sub-Agents)

**Status:** Proposed — 2026-06-04
**Related:** ADR-0085 (intra-turn tool-result compression — **composes inside each discovery sub-agent's own tail; this ADR owns the parallelism/context-isolation axis, ADR-0085 owns the tail-digest axis**), ADR-0082 (tier-aware model selection — decomposed sub-agents run on the `sub_agent` non-thinking tier; verify interaction), ADR-0084 (partially supersedes ADR-0082 for the pedagogical routing question), ADR-0077 (`artifact_draft` plan/generate split — the generation path this ADR deliberately leaves intact), ADR-0063 (primitive tools & action-boundary governance — owns the Stage 5 decomposition matrix), ADR-0036 (expansion controller — the plan→dispatch→synthesize machinery extended here), ADR-0074 (identity / joinability — emit-site discipline new events inherit), FRE-468/FRE-473 (cache_control ≤4 clamp — the cost context this sits beside)
**Implements:** FRE-476 (project: *Turn Cost & Latency Optimization (artifact builds)*)
**Evidence:** `docs/research/2026-06-04-artifact-turn-cost-latency-forensics.md` (trace `a0a07227`), code audit of the live expansion/sub-agent substrate (see §"What already exists")

---

## Context

### The measured problem

FRE-469 fixed a classifier misroute so artifact/build turns route to `TOOL_USE` (tool-iteration budget 25) and the artifact ships. The first successful such turn (`a0a07227`, claude-sonnet-4-6, cloud path) is *correct* but expensive and slow: **23 LLM rounds, ~$1.14, 14 m 34 s wall-time, 768 k full-price input tokens**. The research doc's per-round forensics (§4–6) isolate three structural causes; this ADR attacks the one the *other* levers cannot:

```
 #  fresh_in  cache_rd    out  lat_s   phase
 2    14,803         0    173    3.1   ┐ DISCOVERY — 17 serial tool rounds;
 8    23,674    35,496    171    3.3   │ one growing parent context;
18    47,366    91,802    133    4.5   ┘ fresh_in climbs monotonically 14k→71k
19    50,778    92,092  3,189   70.9   ┐ GENERATION TAIL
21     2,596         0 16,384   96.1   │ artifact_draft sub-agent — OUTPUT-CAPPED at 16,384
22    56,199    94,998 14,835  214.0   ┘ spill continuation (worst single call)
```

The discovery phase is **17 serial rounds, each re-sending one monotonically growing context** — the parent tail reaches ~71 k fresh tokens by the end. Single-strategy execution cannot parallelize those rounds, nor isolate their context, because the gateway assessed `strategy=single` (`reason=tool_use_single`).

### The latent constraint (research doc §6)

`request_gateway/intent.py:309` hard-pins `complexity = Complexity.SIMPLE` for every `TOOL_USE` turn. `request_gateway/decomposition.py:103` therefore returns `SINGLE, "tool_use_single"` **unconditionally** — the `TOOL_USE` case has no complexity branch at all, unlike `ANALYSIS`/`PLANNING`. A turn that does 32 tool calls and builds a multi-section artifact is assessed "simple" and forced down the single-agent path. The complexity pin actively **blocks** the decomposition that would relieve the serial-discovery cost — a side-effect of FRE-469's minimal fix, exactly the kind of thing a forensic pass exists to catch.

### What already exists (and the real gap)

A code audit of the execution substrate is load-bearing for this ADR, because "route artifact builds to HYBRID" is **not** a config flip onto working machinery:

- **The gateway → expansion wiring exists.** In `enforced` mode (the production default, `settings.orchestration_mode`), `executor.py:1645` already routes `HYBRID`/`DECOMPOSE` into `ExpansionController.execute()` (`expansion_controller.py`): LLM planner → plan validation → concurrent dispatch → synthesis context appended → `TaskState.LLM_CALL`. So a routing change *reaches* a real dispatcher.
- **But the dispatched sub-agents cannot run tools.** `SubAgentMode.TOOLED_SEQUENTIAL` exists (`expansion_types.py:26`) and `run_sub_agent` branches into `_run_tooled_loop` (`sub_agent.py:90`), **but that loop is a stub**: `sub_agent.py:212` reads `TODO: Parse tool calls from response when LLM client exposes them. For now, return the response directly.` It performs **one** LLM call and returns — no tool execution, no iteration. **TOOLED_SEQUENTIAL discovery does not work today.** The other mode, `PARALLEL_INFERENCE`, is tool-less by definition.
- **Generation already has its own path.** `artifact_draft` (ADR-0077, `tools/artifact_tools.py`) dispatches a single `sub_agent` HTML-generation call with **`_DRAFT_MAX_TOKENS = 16384` hardcoded** (`artifact_tools.py:647`) — the exact ceiling round 21 hit. Raising/parameterizing that cap is **FRE-478's** scope, not this ADR's.

So the gap FRE-476 names has two parts: a **classification gap** (the complexity pin, small) and an **execution-capability gap** (tool-using discovery sub-agents do not exist, load-bearing). The research doc frames FRE-476 as "highest ceiling, most design surface" precisely because of the second part.

### Scope boundary (decided with the owner)

This ADR commits to **discovery-decomposition only**:

- **In scope:** unpin `TOOL_USE` complexity, route high-complexity artifact builds to `HYBRID`, and build the tool-using discovery sub-agent so decomposed discovery actually runs — concurrent sub-agents, each in its own context slice, each returning a **digest** to the parent.
- **Out of scope (deferred):** *generation sectioning* (drafting artifact parts concurrently then assembling). Generation stays the single `artifact_draft` path; the output-cap pain is owned by **FRE-478**. Generation-sectioning becomes a follow-up *only if* the discovery-decomposition A/B shows the generation tail is still the dominant residual cost. Building it speculatively now is unjustified surface.

### Relationship to the sibling levers (compose, do not double-claim)

The four project tickets attack overlapping findings; honest attribution matters for the before/after measurement:

| Lever | Owns | Attacks |
|---|---|---|
| **FRE-475 / ADR-0085** | intra-turn tool-result tail digest | Finding 1 (uncached re-billed tail) **within a single context** |
| **FRE-476 / this ADR** | parallelism + context isolation across sub-agents | Finding 1's *parent-tail growth* + the serial-discovery **wall-time** |
| **FRE-477** | discovery batching via compound bash | round count (fewer round-trips) |
| **FRE-478** | artifact-draft output cap | Finding 2's 16 k spill |

The seams: ADR-0085 digests **compose inside each discovery sub-agent's own tail** (each sub-agent still accretes tool output; FRE-475 keeps that bounded). This ADR keeps the **parent** tail bounded by returning digests rather than raw transcripts. **No hard ordering dependency** — either can ship first; they multiply rather than block. This ADR does **not** re-claim the within-context fresh-input reduction already attributed to FRE-475.

---

## Decision

Make `TOOL_USE` complexity a function of the request (not a constant), route high-complexity artifact builds through the existing `HYBRID` expansion path, and implement a real **tool-using discovery sub-agent** so decomposed discovery executes concurrently with isolated context, each sub-agent returning a digest to the parent. Generation is unchanged.

### D1 — Unpin `TOOL_USE` complexity; add an artifact-build complexity sub-signal

`intent.py` stops hard-setting `complexity = Complexity.SIMPLE` for `TOOL_USE`. Instead:

- **Plain tool turns** (`search for X`, `read file Y`, `check ES health`) run through the existing `_estimate_complexity()` and, being short single-action messages, resolve to `SIMPLE` — **preserving SINGLE routing with no regression** (the explicit FRE-256/210/469 guarantee).
- **Artifact-build intent** is promoted to a distinct sub-signal. The existing artifact-build alternation inside `_TOOL_INTENT_PATTERNS` (`intent.py:110` — `(build|make|create|generate) … (guide|dashboard|html|interactive|…)`) is factored into a dedicated `_ARTIFACT_BUILD_PATTERNS`. When it matches, the classifier appends an `artifact_build` signal and **biases complexity upward** — flooring it at `MODERATE` (and allowing `COMPLEX` when the message's own action-verb/length heuristics already reach it). This targets exactly the measured class without re-tagging plain lookups.

Rationale (decided with the owner): message heuristics alone are too weak for this class — the trace's prompt ("explain the internals and build an interactive HTML guide") need not trip the `≥3 action-verb` threshold. The artifact-build signal already fires deterministically in the regex bank; promoting it to a complexity bias is the precise, reviewable lever. (Resolves research-doc open question #3.)

### D2 — Decomposition matrix: branch `TOOL_USE` on complexity

`decomposition.py:_apply_matrix` replaces the unconditional `TOOL_USE → SINGLE` with a complexity branch mirroring `ANALYSIS`/`PLANNING`:

| TaskType | Complexity | Strategy | Reason |
|---|---|---|---|
| `TOOL_USE` | `SIMPLE` | `SINGLE` | `tool_use_simple_single` *(no regression)* |
| `TOOL_USE` | `MODERATE` | `HYBRID` | `tool_use_moderate_hybrid` |
| `TOOL_USE` | `COMPLEX` | `HYBRID` | `tool_use_complex_hybrid` |

**`HYBRID`, not `DECOMPOSE`, for both moderate and complex.** Discovery slices of an artifact build are *independent areas of investigation* (e.g. "the request-flow path," "the memory subsystem," "the tool registry") — they parallelize, with no inter-slice dependency ordering. `HYBRID` is the concurrent-fan-out strategy (`types.py:46`); `DECOMPOSE` is for sequential sub-task chains. Reserving `DECOMPOSE` for a future dependency-ordered discovery keeps the first cut conservative and the parallelism win clean.

The existing resource-pressure guard is untouched and **takes precedence**: when `governance.expansion_permitted` is false or `expansion_budget <= 0` (ALERT/DEGRADED/LOCKDOWN/RECOVERY), Stage 5 still forces `SINGLE` (`decomposition.py:46–58`) — a high-complexity artifact build under homeostatic pressure **gracefully degrades to the serial single-agent path** rather than fanning out when the system is already stressed. This is the intended safety property, stated explicitly.

### D3 — Implement the tool-using discovery sub-agent (the load-bearing piece)

`_run_tooled_loop` (`sub_agent.py:170`) is currently a stub that returns the first LLM response without executing tools. This ADR makes it a real bounded agentic loop:

- **Execute tool calls through a shared dispatch boundary.** When the model returns tool calls, the loop must dispatch them through the **same `ToolRegistry` + governance-evaluation path the primary executor uses** — not a parallel implementation — so tool permissions, action-boundary governance (ADR-0063), and per-call telemetry/`trace_id` threading (ADR-0074) are inherited rather than re-derived. The current stub (`sub_agent.py:199–221`) has no tool-call parsing or dispatch bridge at all; the implementation extracts the shared dispatch into a callable the sub-agent loop and the primary both invoke (the exact factoring is an implementation detail, but "one dispatch path, two callers" is a contract of this ADR — a forked dispatcher that re-implements policy is a defect). Results append to the sub-agent's own message list and it re-prompts, until the model returns a final answer or the iteration ceiling is hit.
- **Bounded iterations, configurable.** The hardcoded `max_iterations: int = 3` becomes `settings.sub_agent_max_tool_iterations` (default tuned against the per-slice depth observed in the A/B; the parent did 17 rounds across the whole turn, so a handful of slices at ~4–6 iterations each is the target shape). The ceiling bounds worst-case sub-agent runtime against `worker_timeout_seconds`.
- **Constrained tool surface.** Discovery sub-agents receive **read-only discovery tools** (`bash`, `read`, search) via `SubAgentSpec.tools` from the planner — never mutating tools (`write`/`edit`/`artifact_write`) and never the expansion path itself (no recursive fan-out). The planner already carries `tools` and `mode` per task (`expansion_controller.py:346–348`); the planner prompt is extended to emit `TOOLED_SEQUENTIAL` discovery slices for artifact-build queries.
- **Return a digest, not a transcript** (D4).

This is the piece that turns the routing change from a no-op into a working capability. It is explicitly named as real implementation surface, not a config flag — consistent with ADR-0082 D3's discipline of costing executor state-surgery honestly.

### D4 — Context isolation + digest return (the cost/wall-time mechanism)

The win comes from *what crosses the sub-agent boundary*, not merely from fan-out:

- **Each discovery sub-agent holds its own context slice.** Its accreting tool output lives in *its* message list, not the parent's. The parent's tail therefore never grows to 71 k — it receives only digests. This is the parent-tail bound that single-strategy cannot achieve.
- **The sub-agent returns a compressed summary**, as `SubAgentResult` already contracts (`sub_agent.py` header: "only the summary enters the primary agent's synthesis context"). The digest is the load-bearing facts the parent needs to generate the artifact (APIs, structures, file references), not raw bash/read dumps.
- **ADR-0085 composes inside the sub-agent.** Within each sub-agent's bounded loop, FRE-475 tail-digestion (when live) keeps *that* loop's fresh input bounded too — the two levers stack: ADR-0085 bounds the *intra-context* tail, this ADR bounds the *parent* tail.
- **Honest caveat (the primary risk) — only the parent-tail bound is deterministic.** The **parent-tail reduction** is structural and guaranteed (digests cross the boundary, not transcripts). The **wall-time** win is *expected but measurement-gated, not guaranteed*: on the single-GPU local host `primary` and `sub_agent` share one llama-server endpoint served single-threaded, and `settings.worker_global_timeout_seconds` (`settings.py:443`, "Max total time for all sub-agent workers combined — serial GPU") encodes exactly that serialization, so concurrent dispatch parallelizes wall-time only where the tiers have separate inference capacity (a cloud profile or a second slot) — the same conditional ADR-0082 D4 states. The **net total-token** win is also *not* automatic: N sub-agent contexts plus a planning call plus synthesis can exceed one serial context if slices **overlap** in what they rediscover, or if decomposition is too fine-grained. Both the wall-time and net-cost axes are therefore **gated on measurement** (Verification §1), mitigated by (a) a small slice count, (b) non-overlapping slice goals from the planner, and (c) digest-only returns. The ADR claims the parent-tail bound as the deterministic win and treats wall-time and net cost as measured hypotheses — the FRE-433/434 *measure-don't-assert* discipline.

### D5 — Discovery → generation handoff; generation stays single (scope boundary)

The flow end-to-end:

1. Gateway routes high-complexity artifact build → `HYBRID` (D1/D2).
2. `ExpansionController` plans discovery slices, dispatches concurrent tool-using sub-agents (D3), each returns a digest (D4).
3. The existing enforced-mode path appends the synthesis context (the digests) as a user message and returns `TaskState.LLM_CALL` (`executor.py:1674–1700`).
4. The parent's synthesis turn — with `artifact_draft` available in its tool set — calls `artifact_draft` against the assembled digests, which generates the HTML via its own `sub_agent` call (ADR-0077, **unchanged**).

No new generation machinery. The output-cap pain at step 4 is **FRE-478's** to fix. The seam between "expansion synthesis" and "artifact generation" is the existing tool-call boundary; this ADR does not move it.

### D6 — Tier interaction (ADR-0082) and budget accounting

- **Tier.** Discovery sub-agents run on the `sub_agent` (non-thinking, `max_concurrency: 3`) tier the expansion path already uses — exactly the focused-single-task workload ADR-0033 built that tier for, and the cell ADR-0082 D2 flagged as risky (deep *non-thinking* tool loops) is bounded here by the iteration ceiling (D3) and the read-only surface. **Whether non-thinking discovery degrades digest quality vs the thinking primary is a verification gate (§2), not an assumption.** The parent planning and final synthesis remain on `primary`.
- **Budget.** The `TOOL_USE` tool-iteration budget (25, `settings.py:181`) is the *primary* agent's. With decomposition, discovery iterations move into sub-agents, each bounded by `sub_agent_max_tool_iterations` (D3) and the overall fan-out bounded by `expansion_budget_max` (default 3, `settings.py:412`). The ADR does not change these defaults; it notes the accounting shift so the cost-gate attribution (per ADR-0082 D5's per-tier dimension, when live) reflects discovery moving to the `sub_agent` class.

### D7 — Observability, configuration & rollout

- **Joinability (ADR-0074).** The decomposition decision is already emitted (`decomposition_assessed` carries `task_type`/`complexity`/`strategy`/`reason`). The new `tool_use_*_hybrid` reasons and the `artifact_build` intent signal make the routing change queryable. New discovery-sub-agent events (start/iteration/complete with digest size) carry `session_id` + `trace_id` from `TraceContext`; `joinability_probe.py` must show **no orphans** post-deploy.
- **Config** (in `settings`, never hardcoded): `artifact_decomposition_enabled` (the rollout flag, default **off**), `sub_agent_max_tool_iterations` (D3), and reuse of existing `expansion_budget_max` / `worker_timeout_seconds` / `sub_agent_max_tokens`.
- **Rollout.** Flag-gated; enabled only after the before/after A/B clears the gate — the FRE-433/434 *measure → flag → verify → enable* sequence. The classification change (D1/D2) and the execution change (D3/D4) ship behind the **same** flag so a high-complexity assessment never routes to a stub.
- **Rollback.** Setting `artifact_decomposition_enabled=false` fully restores the current `TOOL_USE → SINGLE` path: the gateway re-pins/ignores the artifact-build complexity bias and stops routing to `HYBRID`, and the implemented `_run_tooled_loop` is inert for non-routed traffic (no `TOOLED_SEQUENTIAL` discovery is dispatched). Rollback is a single config change with no schema or data migration — the standard escape hatch for this cost line.

---

## Consequences

### Positive

- Removes the latent constraint (§"latent constraint"): a turn doing 32 tool calls is no longer assessed `SIMPLE`.
- **Parent-tail bound (the deterministic win):** the parent never accretes the 71 k discovery tail; it receives digests. Composes with (does not duplicate) ADR-0085's within-context digest.
- **Wall-time (expected, measurement-gated):** the 17 serial discovery rounds parallelize across concurrent sub-agents — attacks the dominant non-generation latency *where the tiers have separate inference capacity*; near-zero on the single-GPU local host (D4 caveat).
- Turns the dormant `TOOLED_SEQUENTIAL` mode into a working capability, reusing the existing planner→dispatch→synthesize substrate rather than a parallel mechanism.
- Deterministic, reviewable routing (D1/D2) consistent with the gateway matrix it extends; degrades gracefully under homeostatic pressure (D2).

### Negative / tradeoffs

- **Net-cost is not guaranteed (primary risk).** Fan-out can raise *total* tokens if slices overlap or decomposition is too fine — the cost win is a measured hypothesis (D4, Verification §1), not an assertion.
- **Real implementation surface, not a flag.** D3 implements actual tool execution inside sub-agents (the stub today). This is the bulk of the work and carries the correctness risk of a second agentic loop.
- **Digest fidelity risk.** A discovery digest that drops a load-bearing fact makes the parent generate an artifact against an absent detail. Mitigated by digest sizing, the read-only discovery surface, and the side-by-side artifact eval (Verification §2).
- **Non-thinking discovery quality (ADR-0082).** Bounded by iteration ceiling + read-only tools; gated by §2, not assumed.
- **More moving parts on a hot path** — flag-gated and A/B-measured before enable, like every change in this cost line.

---

## Verification

Measured with the FRE-433 reproducible recipe (research doc §9) — before/after per-round token-curve tables, never single anecdotes:

1. **Wall-time and parent fresh-input both reduced** — re-run an equivalent artifact-build turn; parent `fresh_in` no longer climbs to ~71 k (digests bound it) and wall-time drops vs the `a0a07227` baseline (concurrent discovery). Report the full per-round table **and** total-token delta — and state honestly whether net cost fell, held, or rose (D4 caveat).
2. **No artifact-quality regression** — side-by-side eval of artifact output (the `feedback_always_include_references` + side-by-side discipline): correctness and completeness unchanged vs the serial baseline, including digest-driven generation vs full-context generation.
3. **No simple-tool regression** — the FRE-256 / FRE-210 / FRE-469 fixtures: plain `TOOL_USE` lookups still assess `complexity=SIMPLE` → `strategy=single`. Unit tests in `test_intent.py` cover the new complexity outcomes (artifact-build → MODERATE/COMPLEX; plain lookup → SIMPLE) and `test_decomposition.py` covers the new matrix cells.
4. **Discovery sub-agent actually runs tools** — an integration test asserts a `TOOLED_SEQUENTIAL` sub-agent executes ≥1 tool call and returns a digest (guards against regressing to the stub).
5. **Flag guard** — with `artifact_decomposition_enabled=false`, a high-complexity artifact-build message **still routes to `SINGLE`** (no `HYBRID`, no `TOOLED_SEQUENTIAL` dispatch) — asserted explicitly so the off-by-default guarantee and rollback path (D7) are verified, not assumed.
6. **Governance degradation** — under withheld expansion, a high-complexity artifact build forces `SINGLE` (graceful fallback), asserted in a gateway test.
7. **Joinability (ADR-0074, gating)** — `joinability_probe.py` reports no orphans for the new discovery-sub-agent events; the routing decision is sliceable by `artifact_build` signal and `tool_use_*_hybrid` reason.
8. **Backend-aware truth source** — per FRE-433, cache/cost read from the backend's own counters (local `timings.cache_n`, cloud `cache_read_input_tokens`), not a single conflated ES field.
9. `make test` / `make mypy` / `make ruff-check` / `make ruff-format` clean.

---

## Open decisions (data-gated)

1. **Slice count and `sub_agent_max_tool_iterations`** — start conservative (≤ `expansion_budget_max` slices, ~4–6 iterations each) and tune against the per-round curve; too-fine decomposition inverts the cost win (D4).
2. **DECOMPOSE vs HYBRID for `COMPLEX`** — this ADR routes both moderate and complex to `HYBRID`; if a dependency-ordered discovery shape emerges (slice B needs slice A's finding), revisit `DECOMPOSE` for `COMPLEX`.
3. **Generation sectioning** — deferred (scope boundary). Reopen only if §1 shows the generation tail dominates the residual after discovery-decomposition + FRE-478.
4. **Interaction with FRE-475 rollout order** — none required (compose); confirm the stacked A/B (both flags on) shows multiplicative, not conflicting, behavior when both are live.

---

## References

- **Implements:** [FRE-476](https://linear.app/frenchforest/issue/FRE-476) · research doc `docs/research/2026-06-04-artifact-turn-cost-latency-forensics.md` (trace `a0a07227`, §6 latent constraint, §7 lever ②)
- **Sibling levers:** FRE-475 / ADR-0085 (intra-turn compression) · FRE-477 (discovery bash batching) · FRE-478 (artifact output cap)
- **Internal:** ADR-0082 (tier-aware model selection) · ADR-0084 (pedagogical routing, partially supersedes ADR-0082) · ADR-0077 (`artifact_draft` plan/generate split) · ADR-0036 (expansion controller) · ADR-0063 (decomposition matrix / action-boundary governance) · ADR-0033 (model taxonomy / `primary`+`sub_agent` tiers) · ADR-0074 (identity / joinability) · FRE-256 / FRE-210 / FRE-469 (tool-routing regression fixtures) · FRE-433/FRE-434 (measurement methodology)
- **Code anchors:** `request_gateway/intent.py:309` (the pin) · `request_gateway/decomposition.py:103` (`tool_use_single`) · `orchestrator/sub_agent.py:170,212` (the TOOLED_SEQUENTIAL stub) · `orchestrator/expansion_controller.py` (plan→dispatch→synthesize) · `orchestrator/executor.py:1645` (HYBRID enforced-mode wiring) · `tools/artifact_tools.py:647` (`_DRAFT_MAX_TOKENS`, FRE-478's territory)
