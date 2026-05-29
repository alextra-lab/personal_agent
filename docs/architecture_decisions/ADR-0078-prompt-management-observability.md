# ADR-0078: Prompt Management and Observability

**Status:** Proposed
**Date:** 2026-05-28
**Issue:** (Epic — see phased tickets P0–P5 in Linear, FrenchForest)
**Supersedes:** FRE-267 (per-session binary thumbs — see D5)
**Related:** ADR-0038 (context compressor + prefix ordering), ADR-0054 (feedback stream bus convention), ADR-0057 (insights pattern analysis), ADR-0058 (self-improvement pipeline stream), ADR-0074 (end-to-end traceability / identity tuples)

---

## Context

A full audit of the Seshat harness on 2026-05-28 found the following:

### Prompt landscape

The harness contains two structurally distinct classes of prompt:

**Leaf prompts** (~10, self-contained strings):

| Prompt | Location | Est. tokens |
|--------|----------|-------------|
| Entity extraction template | `second_brain/entity_extraction.py:28` | ~1,075 |
| Context compressor system | `orchestrator/context_compressor.py:34` | ~975 |
| Tool-use rules (`_TOOL_RULES`) | `orchestrator/prompts.py:46` | ~375 |
| `TOOL_USE_NATIVE_PROMPT` | `orchestrator/prompts.py:60` | ~450 |
| `TOOL_USE_PROMPT_INJECTED` (incl. few-shots) | `orchestrator/prompts.py:70` | ~600 |
| `get_tool_awareness_prompt()` | `orchestrator/prompts.py:104` | varies |
| Reflection DSPy signature | `captains_log/reflection_dspy.py:76` | ~500 |
| Manual reflection fallback | `captains_log/reflection.py:158` | ~575 |
| HTML generation system | `tools/artifact_tools.py:656` | ~500 |
| Router system prompt | `orchestrator/prompts.py:21` | ~140 |
| Gateway persona (`_SYSTEM_PROMPT`) | `gateway/chat_api.py:39` | ~35 |

**The orchestrator composed prompt** (not a string, not a file): assembled imperatively in `orchestrator/executor.py:1835–2244` by ordered conditional concatenation. It incorporates: deployment context, operator stanza, skill-index entries (from `docs/skills/*.md`, capped at 2,048 tokens), memory section, tool-awareness prefix, tool-use rules, decomposition instructions. It is the **largest prompt per turn** and is currently invisible — only 100-character previews are logged at `executor.py:2226`.

### Observability gaps

1. **No prompt identity on any call**: `llm_client/telemetry.py` captures latency, tokens, cache hits, cost — but no prompt callsite, no component list, no hash. A/B comparison of prompt versions is impossible.

2. **No hash measures the *stable* prefix**: `context_window.py:353` defines `compute_prefix_hash(message)`, hashing `output_messages[0]` — the assembled system message *with the per-turn memory section already embedded*. That makes it useless as a cache-prefix signal (it changes every turn with memory). **Correction (2026-05-29):** this function is *not* simply "pointing at the wrong object" — it is also load-bearing for a distinct, separately-tested invariant (head/system message preserved byte-identical across compression & truncation cycles; see `test_kv_cache_stability.py`, `test_within_session_compression.py`). It must be kept. The real gap is that **no** hash isolates the STATIC+SEMI_STATIC cacheable prefix from the dynamic remainder. See D4 (revised).

3. **Third untelemetered call path**: `gateway/chat_api.py` calls `anthropic.AsyncAnthropic().messages.stream()` directly at line 88–93, bypassing `llm_client/telemetry.py` entirely. It also carries a drifted persona (`_SYSTEM_PROMPT` at :39 — a minimal Seshat system message, unrelated to the orchestrator persona).

4. **KV-cache prefix instability — worse than first documented**: ADR-0038 orders orchestrator prompt assembly for prefix stability, but the actual assembly violates it badly. **Correction (2026-05-29, verified line-by-line):** the final system prompt is built by two f-string splices — `executor.py:2193` appends the DYNAMIC `memory_section` into `system_prompt`, then `executor.py:2218` wraps the whole thing as `f"{tool_awareness}\n\n{system_prompt}\n\n{tool_prompt}"`. The resulting byte order is `tool_awareness` (SEMI_STATIC) → operator/skill/deployment (SEMI_STATIC) → `memory_section` (**DYNAMIC, mid-string**) → `tool_prompt`/tool-use rules (**STATIC, ~975 tok, appended *after* the dynamic section**) → decomposition (STATIC). So STATIC content sits *after* DYNAMIC content; there is no single clean static-prefix boundary, and the largest static block (tool rules) is in the least cacheable position. The earlier note (tool-awareness "prepended late at 2171") understated this — the original §1.2 line-range table was also wrong (the splice is at 2218, not 2171–2194). Cache erosion is an active risk, currently unmeasured. P2 will quantify it; the optional composer redesign is the fix.

5. **Token-counting divergence**: two independent heuristics: `words×1.3` in `request_gateway/budget.py:27`; `chars//4` in `orchestrator/context_window.py:15`. `tiktoken` is a transitive dependency but is never imported.

6. **No corpus legibility**: there is no way to read the effective prompts as a human. Prompt text is never persisted. Redundancy, persona drift, bloated few-shots, and dead skills cannot be detected without running the system.

### Missing primitive

All of these gaps share a root: **prompt identity**. Without identity — a stable name for what was sent — there is no attribution, no diffing, no caching signal, no eval joinability. "Central management" is the wrong framing: the leaf prompts are trivially centralizable; the orchestrator prompt is a *composition pipeline* and cannot be a single string. The needed primitive is a schema that names the composition and stamps it on every call.

---

## Decisions

### D1 — Prompt identity is the foundational primitive (extends ADR-0074)

Every LLM call carries a **`PromptIdentity`** tuple alongside the ADR-0074 identity tuple:

```
callsite:            str          # module.function or symbolic name, e.g. "orchestrator.primary"
component_ids:       list[str]    # ordered list of named components assembled for this call
static_prefix_hash:  str          # SHA-256 of the static/stable prefix (components not dependent on turn content)
dynamic_hash:        str          # SHA-256 of the full assembled prompt (including dynamic content)
```

`PromptIdentity` is **not** a new parallel scheme — it is an extension of the existing ADR-0074 identity tuple. It is emitted as additional fields on the existing `model_call_completed` event shape, so all existing joins continue to work.

`callsite` is the primary human-readable key. `component_ids` lets consumers understand *which components were active* for a given call. The two hashes let consumers detect when the effective prompt changed, without persisting the text.

### D2 — Named component taxonomy for the orchestrator prompt

The orchestrator composed prompt is decomposed into a **named component taxonomy**. Each component has:

- A stable `component_id` string (e.g. `"deployment_context"`, `"operator_stanza"`, `"skill_index"`, `"memory_section"`, `"tool_awareness"`, `"tool_use_rules"`, `"decomposition_instructions"`)
- A documented KV-cache position tier: `STATIC` (content never varies across turns for the same deployment), `SEMI_STATIC` (varies at session boundary), or `DYNAMIC` (varies per turn)
- Token count captured at assembly time

The taxonomy also covers leaf prompts: each gets a stable semantic `component_id` and lives in the existing source location (no forced migration). The taxonomy is the authoritative component registry — codified in `docs/specs/PROMPT_MANAGEMENT_SPEC.md` and enforced by the corpus renderer (D3).

Leaf prompts are **not** forcibly migrated to a `prompts/` directory. The dependency chain runs from the taxonomy document outward; the renderer derives the corpus from wherever prompts actually live. If a prompt moves later, only its `component_id` → source location mapping changes.

### D3 — Legibility via corpus renderer (static) + assembled-prompt capture (runtime, debug-gated)

**Static corpus renderer** (P0): a source-derived script (`scripts/render_prompt_corpus.py`) reads every leaf prompt from its source file, reads every `docs/skills/*.md` body, reads every tool description from `config/governance/tools.yaml` + the tool registry, and renders a human-readable annotated document at `docs/reference/PROMPT_CORPUS.md`. Output format: one section per component, with token count (using a unified counter — see D6), source file:line reference, and cache tier. Regenerable on demand; diff is human-readable.

The corpus renderer is the **primary legibility surface**. It enables the core owner use case: "I want to read our prompts." It also enables harness compression: redundancy (tool guidance 3–4× repeated, two drifted personas), bloated few-shots, and dead skills become visible in a single document.

**Assembled-prompt capture** (runtime, debug-gated): when the environment variable `AGENT_PROMPT_DEBUG=true` is set (never on by default), the full assembled system prompt for each call is written to a local debug file. Prompt text is **never persisted by default** — it contains personal memory content and user data. The debug path is for local development / iteration only; it is excluded from git (`.gitignore`) and never shipped to ES or the bus.

### D4 — Telemetry stamping on all call paths, including the gateway fork; add static/dynamic prefix hashing (revised 2026-05-29)

**Telemetry stamping**: `emit_model_call_completed()` in `llm_client/telemetry.py` gains a `prompt_identity: PromptIdentity` parameter. Both `LiteLLMClient` and `LocalLLMClient` pass it. The gateway path (`gateway/chat_api.py`) is wired through the canonical telemetry emit — it is the only call path that currently bypasses it. The gateway `_SYSTEM_PROMPT` persona is assigned `callsite="gateway.chat"` and a fixed `component_ids=["gateway_persona"]`.

**Hashing — revised after caller analysis.** The original decision ("`compute_prefix_hash` becomes the implementation of `static_prefix_hash`; no new hash scheme") is **reversed**. `compute_prefix_hash(message)` is kept *intact* — it guards the head-preservation invariant (see Context #2) and changing its signature breaks ~8 real assertions. Instead, a **new module** `llm_client/prompt_identity.py` owns prompt-identity hashing:
- `static_prefix_hash` = SHA-256[:16] of the **literal cacheable prefix** — the assembled bytes up to the first DYNAMIC component (`memory_section`). Given the broken assembly order (Context #4), this honestly captures `tool_awareness + operator/skill/deployment` and **excludes** the post-memory STATIC tool rules, *by design*: those bytes are not in a cacheable prefix position in the current composer. This makes the erosion measurable rather than hiding it.
- `dynamic_hash` = SHA-256[:16] of the full assembled prompt (all tiers).
- A shared `_short_hash(s: str) -> str` helper backs both; the executor computes the static prefix at assembly time (capturing `tool_awareness` + the inner system prompt *before* the `memory_section` append) and constructs the `PromptIdentity`.

**Consequence for the AC**: the P1 acceptance test asserts `static_prefix_hash` changes when prefix-resident STATIC/SEMI_STATIC content changes and is stable when only `memory_section` changes. A change to the *post-memory* tool rules will **not** move the hash — correct, because those bytes genuinely aren't in the stable prefix today. P2 surfaces this; the optional composer redesign relocates them.

### D5 — Per-turn 0–3 value rating carrying prompt identity, superseding FRE-267

FRE-267 ("per-session binary thumbs feedback") is **canceled**. Its reusable backend design — dual-write to `telemetry/user_feedback/` + durable file, `stream:user.feedback_received` Redis bus event, Insights consumer — is **salvaged and extended** for this initiative.

The new design is **per-turn, per-assistant-message**, using a **0–3 anchored scale**:

| Score | Anchor |
|-------|--------|
| 0 | No value — response missed the mark entirely |
| 1 | Low value — partial or mostly unhelpful |
| 2 | Meets expectation — adequate, does the job |
| 3 | Wow — exceeded expectation, surprising quality |

Each rating record carries the full `PromptIdentity` tuple from the rated turn, making it a **ground-truth label joinable to the prompt version that produced the response**. This is the capstone of the six value loops: without identity, per-turn ratings are anecdote; with identity, they are a training signal.

**UI placement**: one rating control per assistant message in the PWA, co-located with the message — not in the footer or as a session-level control. The control is compact (e.g., 4 labeled icon buttons). It persists the rating asynchronously; the message is not blocked.

**Data model** (see spec for full schema):
- `UserTurnRating` event — fields: `turn_id`, `session_id`, `trace_id`, `rating` (0–3), `prompt_identity` (the full tuple), `created_at`
- Dual-write: durable NDJSON file + `stream:user.feedback_received` bus publish + ES index `user_turn_ratings`
- `DelegationOutcome.user_satisfaction` (existing 1–5 field in `delegation_types.py`) is a separate concept and is not replaced or merged.

### D6 — Consumers reuse existing pipelines; token counting unified

**Consumer design**: all six value loops are implemented by routing `PromptIdentity`-stamped telemetry events into existing pipelines — no new infrastructure:

| Loop | Consumer | Integration point |
|------|----------|-------------------|
| Cost/cache attribution | Kibana/ES view sliced by `callsite` + `component_ids` | Extend existing `model_call_completed` index |
| Cache-erosion alarm | `static_prefix_hash` distribution shift alert | ADR-0053 gate monitoring pattern |
| Silent-drift detection | `static_prefix_hash` change on skill/tool edit | ADR-0041 bus event → alert |
| Eval attribution | A/B rig results joined on `prompt_identity` | Existing `docker-compose.eval.yml` |
| Agent self-reflection | Captain's Log reads component manifest + ratings | ADR-0058 self-improvement stream |
| Response quality | Per-turn 0–3 rating label | ADR-0054 dual-write + ADR-0057 Insights |

**Token counting unified**: `estimate_tokens(text: str) -> int` becomes a single shared function in `llm_client/token_counter.py`. Implementation: `tiktoken` encoding for the active model family (already a transitive dep via LiteLLM). The two divergent heuristics (`words×1.3` in `budget.py` and `chars//4` in `context_window.py`) are replaced with calls to this function. The corpus renderer uses it for token annotations; the orchestrator assembly loop uses it for budget enforcement; the gateway uses it for cost estimation.

---

## Implementation Phasing

See Linear epic for approved tickets. Dependency spine:

```
P0 Corpus renderer (legibility + component taxonomy)
  └─ P1 Prompt identity primitive (stamp + fix prefix hash + gateway coverage)
       ├─ P2 Cost/cache attribution + drift dashboards
       ├─ P3 Per-turn 0–3 rating (supersedes FRE-267)
       │    └─ P4 Eval attribution (A/B + ratings as metric)
       └─ P5 Agent self-reflection on composition
```

P0 is independently shippable and immediately valuable (legibility, harness compression). P1 is the gate for all downstream loops. P2–P5 are independent after P1. Optional: Composer redesign for KV-cache prefix stability — gated on P2 data proving actual erosion magnitude.

---

## Alternatives Considered

### A1 — Centralized prompts directory (`prompts/` registry)

Move all prompts to a single directory with a loader/registry. Rejected for the orchestrator composed prompt: it is a *composition pipeline*, not a string. A registry helps the easy part (leaf prompts) and hides the hard part. The taxonomy + corpus renderer delivers the same legibility benefit without forcing a structural migration that breaks the KV-cache ordering ADR-0038 depends on.

### A2 — Full prompt versioning in a database

Store every unique prompt text with a content-addressed ID, like a mini git for prompts. Rejected: privacy (prompts contain personal memory/user content), operational complexity, and diminishing returns. Content hashes + identity stamps give 80% of the analytical value with zero storage of user-sensitive text.

### A3 — DSPy-driven optimization pipeline (automated)

Use the existing `dspy_adapter.py` foundation to auto-optimize prompts via DSPy's teleprompters. Deferred: requires the ground-truth labels (P3 ratings) and eval attribution (P4) first. D1–D3 lay the groundwork; DSPy optimization is a natural P6 once the label dataset exists.

### A4 — Per-session binary thumbs (FRE-267 original design)

One thumbs-up/down per session. Rejected on three axes: (a) too coarse to diagnose which turn drove a bad session; (b) no prompt identity attached, so it is anecdote; (c) per-session recency bias (the last turn dominates). The per-turn 0–3 design supersedes on all three.

---

## Consequences

**Positive:**
- "I want to read our prompts" becomes `make render-prompt-corpus` — a 5-second local operation producing a single human-readable document.
- Harness compression is now tractable: redundant tool guidance, persona drift, dead skills, and bloated few-shots are visible in a diff.
- Every LLM call becomes attributable to a named prompt version. A/B analysis of prompt changes is a Kibana filter, not a code archaeology exercise.
- A real `static_prefix_hash` (new `prompt_identity.py`, distinct from the retained `compute_prefix_hash`) measures the actual cacheable prefix; cache erosion — including the post-memory STATIC tool rules — becomes a metric with a dashboard.
- The gateway call path joins the rest of the harness for cost tracking and observability — the third dark path closes.
- Per-turn 0–3 ratings are the first user-sourced ground-truth labels; they become the join key for the self-improvement loop.
- Token counting becomes consistent; budget enforcement and context window management agree.

**Negative / tradeoffs:**
- P0 corpus renderer is a script with ongoing maintenance burden: whenever a new leaf prompt is added, its source location must be registered. The taxonomy document is the authoritative registry — discipline required.
- `PromptIdentity` adds ~4 fields to every `model_call_completed` event. Negligible payload increase given the existing event size.
- `AGENT_PROMPT_DEBUG=true` mode writes full prompt text to local disk. Must be documented prominently; accidental activation in a shared environment would write user-sensitive content to a local file. Mitigation: only functional if `AGENT_ENV=development`.
- Fixing `compute_prefix_hash` will change the hash values in any existing metrics. Intentional break — the old values were measuring the wrong thing.
- Unified token counting with `tiktoken` adds ~20ms to first-call initialization (cold LRU cache load). Negligible in practice; warm calls are sub-millisecond.

**Explicitly out of scope:**
- Automated prompt optimization (DSPy teleprompters) — deferred to post-P4.
- Composer redesign for KV-cache prefix stability — gated on P2 data.
- Backfilling prompt identity on historical telemetry rows.
- Exporting prompt text to any external system.

---

## Verification

- P0: `scripts/render_prompt_corpus.py` runs without error; `docs/reference/PROMPT_CORPUS.md` renders every leaf prompt with source reference and token count; diff is human-readable; regenerating produces identical output on a clean working tree.
- P1: `model_call_completed` events in ES carry `prompt_callsite`, `prompt_component_ids`, `static_prefix_hash`, `dynamic_hash`; gateway path events now appear in ES (zero-gap verification: no model calls in gateway logs without a matching ES event); `derive_prompt_identity` unit test shows `static_prefix_hash` changes when prefix-resident STATIC/SEMI_STATIC content changes and is stable when only `memory_section` changes; `compute_prefix_hash` retained and its head-preservation tests still green.
- P2: Kibana view shows per-`callsite` token/cache breakdown; a deliberately churned static prefix triggers the drift alert.
- P3: `UserTurnRating` records persisted with `prompt_identity` tuple attached; PWA control visible per assistant message; bus event increments; end-to-end test: submit a 0 rating → event on `stream:user.feedback_received` → ES record with non-null `prompt_callsite`.
- P4: A/B eval run reports mean rating per `static_prefix_hash` bucket.
- P5: At least one Captain's Log reflection references a named component from the taxonomy.
