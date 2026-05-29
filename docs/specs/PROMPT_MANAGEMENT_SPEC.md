# Prompt Management & Observability — LLD / Specification

> **ADR:** ADR-0078
> **Status:** Draft — 2026-05-28
> **Phases:** P0 Corpus Renderer → P1 Identity Primitive → P2 Attribution/Drift → P3 Per-Turn Rating → P4 Eval Attribution → P5 Self-Reflection

---

## 1. Component Taxonomy

This section is the authoritative registry of named prompt components. Every component listed here maps to a `component_id` string used in `PromptIdentity.component_ids` and in corpus renderer output.

### 1.1 Leaf Prompts

| `component_id` | Source file | Line | Cache tier | Est. tokens |
|----------------|-------------|------|------------|-------------|
| `entity_extraction_template` | `second_brain/entity_extraction.py` | 28 | SEMI_STATIC | ~1,075 |
| `entity_extraction_system` | `second_brain/entity_extraction.py` | 23 | STATIC | ~70 |
| `context_compressor_system` | `orchestrator/context_compressor.py` | 34 | STATIC | ~975 |
| `router_system_prompt` | `orchestrator/prompts.py` | 21 | STATIC | ~140 |
| `tool_rules` | `orchestrator/prompts.py` | 46 | STATIC | ~375 |
| `tool_use_native_prompt` | `orchestrator/prompts.py` | 60 | STATIC | ~450 |
| `tool_use_injected_prompt` | `orchestrator/prompts.py` | 70 | STATIC | ~600 |
| `tool_awareness_prompt` | `orchestrator/prompts.py` (fn) | 104 | SEMI_STATIC | varies |
| `operator_stanza` | `orchestrator/prompts.py` (fn) | 203 | SEMI_STATIC | varies |
| `reflection_dspy_signature` | `captains_log/reflection_dspy.py` | 76 | STATIC | ~500 |
| `reflection_manual_fallback` | `captains_log/reflection.py` | 158 | STATIC | ~575 |
| `html_generation_system` | `tools/artifact_tools.py` | 656 | STATIC | ~500 |
| `gateway_persona` | `gateway/chat_api.py` | 39 | STATIC | ~35 |

**Cache tier definitions:**
- `STATIC` — content is a module-level constant; never varies at runtime.
- `SEMI_STATIC` — content varies at session boundary (e.g. owner identity, model config, tool set) but is stable within a session.
- `DYNAMIC` — content varies per turn (e.g. session history, memory recall, user message).

### 1.2 Orchestrator Composed Prompt Components

The orchestrator prompt is assembled in `executor.py:1835–2244`. Assembly order determines KV-cache prefix stability — STATIC components must precede SEMI_STATIC which must precede DYNAMIC.

| `component_id` | Content | Cache tier | Assembly line range |
|----------------|---------|------------|---------------------|
| `deployment_context` | VPS/cloud deployment env vars | SEMI_STATIC | 1840–1850 |
| `operator_stanza` | Owner identity + instructions | SEMI_STATIC | 1852–1858 |
| `skill_index` | Active skill metadata + matched skill bodies from `docs/skills/*.md` | SEMI_STATIC | 1860–1993 |
| `memory_section` | Recalled memory nodes for this turn | DYNAMIC | 2126–2149 |
| `tool_awareness` | Tool list + capabilities from `get_tool_awareness_prompt()` | SEMI_STATIC | 2151–2171 |
| `tool_use_rules` | `_TOOL_RULES` + `TOOL_USE_PROMPT_INJECTED` | STATIC | 2171–2194 |
| `decomposition_instructions` | Task decomposition guidance | STATIC | 2176–2194 |

**Cache erosion note (ADR-0078 D4, corrected 2026-05-29):** the assembly line ranges above are approximate; the actual splices are `executor.py:2193` (memory appended) and `executor.py:2218` (`f"{tool_awareness}\n\n{system_prompt}\n\n{tool_prompt}"`). The net effect is worse than "tool_awareness prepended": the **STATIC tool rules (~975 tok) are appended *after* the DYNAMIC `memory_section`**, so the largest static block sits past the dynamic break and cannot be part of a stable cacheable prefix. See §4 for how `static_prefix_hash` is defined against this reality. P2 data will determine whether a composer redesign is warranted.

---

## 2. `PromptIdentity` Schema

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class PromptIdentity:
    callsite: str
    """Symbolic name for this call site, e.g. 'orchestrator.primary', 'captains_log.reflection'."""

    component_ids: tuple[str, ...]
    """Ordered tuple of component_ids assembled for this call."""

    static_prefix_hash: str
    """SHA-256 (16-char hex) of the concatenation of STATIC + SEMI_STATIC components only.
    Computed before dynamic content is appended. Measures KV-cache prefix stability."""

    dynamic_hash: str
    """SHA-256 (16-char hex) of the full assembled prompt string (all tiers)."""
```

### 2.1 Known callsites

| `callsite` | Module |
|------------|--------|
| `orchestrator.primary` | `orchestrator/executor.py` |
| `orchestrator.router` | `orchestrator/executor.py` (intent routing call) |
| `orchestrator.compressor` | `orchestrator/context_compressor.py` |
| `orchestrator.sub_agent` | `orchestrator/sub_agent.py` |
| `second_brain.entity_extraction` | `second_brain/entity_extraction.py` |
| `second_brain.session_summary` | `second_brain/session_summary.py` |
| `captains_log.reflection` | `captains_log/reflection_dspy.py` |
| `gateway.chat` | `gateway/chat_api.py` |
| `artifact.html_generation` | `tools/artifact_tools.py` |

### 2.2 Stamping in telemetry

`emit_model_call_completed()` gains `prompt_identity: PromptIdentity` as a required parameter. The identity fields are flattened into the event payload:

```json
{
  "event": "model_call_completed",
  "session_id": "...",
  "trace_id": "...",
  "span_id": "...",
  "prompt_callsite": "orchestrator.primary",
  "prompt_component_ids": ["deployment_context", "operator_stanza", "skill_index", "memory_section", "tool_use_rules", "decomposition_instructions"],
  "prompt_static_prefix_hash": "a3f7b2c1d4e8...",
  "prompt_dynamic_hash": "9c1e8a4d2f06...",
  "input_tokens": 4200,
  "output_tokens": 312,
  "cache_read_tokens": 3800,
  "latency_ms": 1240,
  "cost_usd": 0.0018
}
```

---

## 3. Corpus Renderer (`scripts/render_prompt_corpus.py`)

### 3.1 Purpose

A CLI script that produces `docs/reference/PROMPT_CORPUS.md` — a human-readable, token-annotated, source-referenced document containing every prompt component. Enables the "I want to read our prompts" use case and serves as the legibility surface for harness compression.

### 3.2 Output format

```markdown
# Prompt Corpus — Seshat Personal Agent
> Generated: {ISO timestamp} · Source revision: {git short SHA}

## Summary
| Component | Cache tier | Tokens | Source |
|-----------|------------|--------|--------|
| entity_extraction_template | SEMI_STATIC | 1075 | second_brain/entity_extraction.py:28 |
...

---

## Leaf Prompts

### `entity_extraction_template`
**Cache tier:** SEMI_STATIC  
**Token count:** 1,075  
**Source:** `second_brain/entity_extraction.py:28`

```
{full prompt text, verbatim}
```

---
...

## Orchestrator Composition Skeleton

**Callsite:** `orchestrator.primary`  
**Assembly order and cache tiers:**

```
[SEMI_STATIC] deployment_context       (~35 tok)   executor.py:1840
[SEMI_STATIC] operator_stanza          (~80 tok)   executor.py:1852
[SEMI_STATIC] skill_index              (~600 tok)  executor.py:1860  ← max 2048 tok budget
[DYNAMIC]     memory_section           (~400 tok)  executor.py:2126  ← varies per turn
[SEMI_STATIC] tool_awareness           (~350 tok)  executor.py:2151  ⚠ inserted after DYNAMIC
[STATIC]      tool_use_rules           (~975 tok)  executor.py:2171
[STATIC]      decomposition_instructions (~120 tok) executor.py:2176
```

**Note:** ⚠ marks components whose cache tier violates the stable-prefix ordering.
Static prefix ends at: `skill_index` (before `memory_section`).

---

## Skill Documents (`docs/skills/*.md`)

### `{skill_name}` ({tokens} tok)
**Source:** `docs/skills/{file}.md`
...
```

### 3.3 Token counter integration

The renderer uses `personal_agent.llm_client.token_counter.estimate_tokens()` (see §7) for all token annotations.

### 3.4 Makefile target

```makefile
render-prompt-corpus:
    uv run python scripts/render_prompt_corpus.py
```

### 3.5 Registration requirement

When a new leaf prompt is added to the codebase, its entry must be added to the taxonomy table in §1.1 of this spec (and the renderer updated to include it). The renderer emits a warning if a `component_id` in the taxonomy has no resolvable source reference.

---

## 4. Static / Dynamic Prefix Hashing  *(revised 2026-05-29)*

**Earlier draft said "fix `compute_prefix_hash`". That is reversed.** A caller audit (Codex-confirmed) found `compute_prefix_hash(message: dict) -> str` is load-bearing for a *separate, tested* invariant — the head/system message is preserved byte-identical across compression & truncation (`test_kv_cache_stability.py`, `test_within_session_compression.py:225/251`). Changing its signature breaks ~8 real assertions. **Keep it as-is.**

**The actual assembly is worse than §1.2 first documented** (verified line-by-line in `executor.py`):
- `executor.py:2193` — `system_prompt = f"{system_prompt}\n{memory_section}"` (DYNAMIC appended into the inner prompt).
- `executor.py:2218` — `system_prompt = f"{tool_awareness}\n\n{system_prompt}\n\n{tool_prompt}"` (SEMI_STATIC `tool_awareness` prepended; STATIC `tool_prompt`/tool rules appended **after** the dynamic memory section).

So the final byte order is: `tool_awareness` → operator/skill/deployment → **`memory_section` (DYNAMIC, mid-string)** → tool rules (STATIC, ~975 tok) → decomposition (STATIC). There is **no single clean static-prefix boundary**, and the largest static block sits in the least cacheable position.

**New design** — a dedicated module `src/personal_agent/llm_client/prompt_identity.py`:

```python
def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]

def derive_prompt_identity(
    callsite: str,
    *,
    static_prefix: str,
    full_prompt: str,
    component_ids: tuple[str, ...] = (),
) -> PromptIdentity:
    return PromptIdentity(
        callsite=callsite,
        component_ids=component_ids,
        static_prefix_hash=_short_hash(static_prefix),
        dynamic_hash=_short_hash(full_prompt),
    )
```

- `static_prefix` = the **literal cacheable prefix**: the assembled bytes up to the first DYNAMIC component. In practice `f"{tool_awareness}\n\n{inner_system_before_memory}"` — captured at assembly time, before the `memory_section` append. By design this **excludes** the post-memory tool rules: those bytes are not in a stable-prefix position today, and pretending otherwise would hide the erosion P2 needs to see.
- `full_prompt` = the complete assembled system prompt (all tiers).

**Call site**: `executor.py` captures `inner_system_before_memory` (the system prompt value immediately before line 2193) and `tool_awareness` (line 2215), builds `static_prefix`, and constructs the `PromptIdentity` after the full prompt is assembled. Non-orchestrator callsites (compressor, entity extraction, gateway, etc.) that have no static/dynamic split pass `static_prefix = full_prompt = system_prompt`.

**AC nuance**: `static_prefix_hash` changes when prefix-resident STATIC/SEMI_STATIC content changes and is stable when only `memory_section` changes. A change to the *post-memory* tool rules does **not** move it — correct, because those bytes aren't in the cacheable prefix under the current composer. The optional composer redesign (gated on P2 data) relocates them.

---

## 5. Gateway Telemetry Coverage (`gateway/chat_api.py`)

**Current state**: `gateway/chat_api.py:88–93` calls `anthropic.AsyncAnthropic().messages.stream()` directly. No `model_call_started` / `model_call_completed` events are emitted. Cost is untracked. The `_SYSTEM_PROMPT` at :39 is a minimal Seshat persona (~35 tokens) that has drifted from the orchestrator persona.

**P1 fix**:
1. Extract the direct `anthropic` call into a thin wrapper that calls through `LiteLLMClient` (or emits the canonical events directly if a direct call is architecturally necessary).
2. Assign `PromptIdentity(callsite="gateway.chat", component_ids=("gateway_persona",), ...)` to this path.
3. The `gateway_persona` component gets a `component_id` entry in the taxonomy and appears in the corpus renderer output.
4. Cost tracking: the gateway call is routed through `cost_gate/` budget reservation like all other call paths.

---

## 6. Per-Turn 0–3 Rating

### 6.1 Data model

```python
@dataclass(frozen=True)
class UserTurnRating:
    turn_id: str           # UUID of the assistant message turn
    session_id: str
    trace_id: str
    rating: int            # 0–3
    prompt_identity: PromptIdentity
    rated_at: datetime
```

### 6.2 Rating anchors

| Rating | Label | Meaning |
|--------|-------|---------|
| 0 | No value | Response missed the mark entirely |
| 1 | Low | Partial / mostly unhelpful |
| 2 | Expected | Adequate — does the job |
| 3 | Wow | Exceeded expectation |

### 6.3 Capture endpoint

`POST /api/v1/turns/{turn_id}/rating` — body: `{"rating": 0–3}`. The `turn_id` is the message UUID from the session. The endpoint:
1. Validates `0 ≤ rating ≤ 3`.
2. Fetches the `prompt_identity` tuple from the ES record for `turn_id` (joined via `trace_id` on `model_call_completed` event).
3. Persists `UserTurnRating` to durable NDJSON file (`telemetry/user_feedback/{date}.ndjson`).
4. Writes to ES index `user_turn_ratings`.
5. Publishes `user.feedback_received` bus event (ADR-0054 convention).

### 6.4 PWA placement

One rating control per assistant message bubble. The control renders as 4 compact buttons labeled 0–3 with the anchor text as a tooltip. Appearance: small, below the message content, right-aligned. State: unrated (default) → rated (one active). Submitting a rating is asynchronous; the message is not blocked. The control remains visible after rating (shows current value). A previously submitted rating can be changed.

The control is **not** in the footer / `ChatInput` area (that slot is occupied by the persistent status meter per feedback memory). It is co-located per message, like a reaction or timestamp.

### 6.5 Insights consumer

The existing ADR-0057 Insights pattern analysis consumer (`insights/`) gains a `UserFeedbackConsumer` that reads from `stream:user.feedback_received` and aggregates:
- Mean rating per `callsite` over rolling 7-day window
- Mean rating per `static_prefix_hash` (prompt version)
- Sessions with mean rating < 1.5 → flag for Captain's Log review

This is modeled on `detect_delegation_patterns()` (ADR-0057 / FRE-247 reference).

---

## 7. Unified Token Counter

### 7.1 Module

`src/personal_agent/llm_client/token_counter.py`

### 7.2 Interface

```python
def estimate_tokens(text: str, model_family: str = "claude") -> int:
    """Return token count estimate for text under the given model family.
    
    Uses tiktoken cl100k_base encoding (adequate approximation for Claude/GPT families).
    Caches the encoding object at module level (cold load ~20ms, warm <1ms).
    
    Args:
        text: The text to count tokens for.
        model_family: Model family hint. Currently only "claude" is used (maps to cl100k_base).
    
    Returns:
        Integer token count.
    """
```

### 7.3 Migration

| Old location | Old formula | Replacement |
|---|---|---|
| `request_gateway/budget.py:27` | `int(len(text.split()) * 1.3)` | `estimate_tokens(text)` |
| `orchestrator/context_window.py:15` | `len(text) // 4` | `estimate_tokens(text)` |

Both sites import from `personal_agent.llm_client.token_counter`. No other callers are changed in P1; the corpus renderer uses `estimate_tokens` directly.

---

## 8. Consumer Design Summary

| Value loop | Mechanism | Prerequisite | ADR integration |
|---|---|---|---|
| Cost/cache attribution | ES query sliced by `prompt_callsite` | P1 | Extend `model_call_completed` Kibana index |
| Cache-erosion alarm | `static_prefix_hash` distribution shift | P1 | ADR-0053 gate monitoring pattern |
| Silent-drift detection | `static_prefix_hash` changes on skill/tool edit → bus event | P1 | ADR-0041 Redis Streams |
| Eval attribution | A/B rig joined on `prompt_identity` | P1 + P3 | `docker-compose.eval.yml` |
| Agent self-reflection | Captain's Log reads component manifest + ratings | P1 + P3 | ADR-0058 self-improvement stream |
| Response quality | Per-turn 0–3 rating with identity | P3 | ADR-0054 dual-write + ADR-0057 Insights |

---

## 9. Privacy and Security Constraints

1. **Prompt text is never persisted in default operation.** Only hashes, lengths, and component IDs are written to ES or the bus. Full text is only written when `AGENT_PROMPT_DEBUG=true` AND `AGENT_ENV=development` — two gated conditions both required.
2. **`docs/reference/PROMPT_CORPUS.md` contains system prompt text** (not user content), but the skill documents may contain implicit knowledge about the owner's tools and workflows. It is committed to the repo and is not secret — the repo is private. Treat it as internal documentation, not as safe for public distribution.
3. **`UserTurnRating` contains `session_id` and `turn_id`** but not message content or prompt text. The rating record is not sensitive beyond standard session attribution.
4. **The debug path** (`AGENT_PROMPT_DEBUG=true`) writes full assembled prompts including personal memory content. These files must be excluded from git (`.gitignore` entry for `telemetry/prompt_debug/`). Only functional in `development` environment.

---

## 10. Acceptance Criteria by Phase

### P0 — Corpus Renderer (Tier-2)

| Gate | Criterion |
|------|-----------|
| Pre-merge | `scripts/render_prompt_corpus.py` runs without error on a clean checkout |
| Pre-merge | `docs/reference/PROMPT_CORPUS.md` contains entries for all 13 leaf prompts listed in §1.1 |
| Pre-merge | Each entry shows token count, source file:line, and cache tier |
| Pre-merge | `make render-prompt-corpus` target exists and works |
| Pre-merge | Re-running the script on an unchanged codebase produces identical output (deterministic) |
| Post-deploy | — (static artifact, no runtime behavior) |
| Future-gate | P1 corpus renderer extended to include `PromptIdentity` hashes per component |

### P1 — Prompt Identity Primitive (Tier-1 schema / Tier-2 impl)

| Gate | Criterion |
|------|-----------|
| Pre-merge | `PromptIdentity` dataclass defined in `llm_client/prompt_identity.py` |
| Pre-merge | `emit_model_call_completed()` signature updated; mypy clean |
| Pre-merge | `LiteLLMClient` and `LocalLLMClient` both pass `prompt_identity` |
| Pre-merge | `gateway/chat_api.py` routed through canonical telemetry emit |
| Pre-merge | `compute_prefix_hash` unit test: hash changes when STATIC/SEMI_STATIC content changes; hash does NOT change when only `memory_section` content changes |
| Pre-merge | `token_counter.py` exists; both old heuristic sites replaced; `make test` passes |
| Post-deploy | Query ES: all `model_call_completed` events in the last 100 calls carry non-null `prompt_callsite` and `prompt_static_prefix_hash` |
| Post-deploy | Query ES: `prompt_callsite = "gateway.chat"` events now present (were absent before) |
| Future-gate | P2 cache-erosion alarm can be wired once `static_prefix_hash` is stable over ≥7 days |

### P2 — Cost/Cache Attribution + Drift (Tier-2)

| Gate | Criterion |
|------|-----------|
| Pre-merge | Kibana index template updated for new `prompt_*` fields |
| Pre-merge | A "per-callsite token/cost" Kibana view exported and committed |
| Pre-merge | Cache-erosion alert defined: fires when `static_prefix_hash` Jaccard similarity between consecutive days drops below 0.9 |
| Post-deploy | Kibana view renders without errors; shows breakdown for at least `orchestrator.primary` and `gateway.chat` |
| Post-deploy | Deliberately mutating a `STATIC` leaf prompt triggers the drift alert within one polling cycle |
| Future-gate | Composer redesign ticket opened if mean cache-hit rate for `orchestrator.primary` is below 60% over 7 days |

### P3 — Per-Turn 0–3 Rating (Tier-2, supersedes FRE-267)

| Gate | Criterion |
|------|-----------|
| Pre-merge | `UserTurnRating` dataclass with full schema defined |
| Pre-merge | `POST /api/v1/turns/{turn_id}/rating` endpoint exists; validates 0–3 range; rejects out-of-range |
| Pre-merge | End-to-end test: submit rating 2 → ES record with non-null `prompt_callsite` → `stream:user.feedback_received` event published |
| Pre-merge | PWA: rating control renders per assistant message; submits without blocking message rendering |
| Pre-merge | FRE-267 Linear ticket marked Canceled with comment linking P3 |
| Post-deploy | Submit 5 ratings in a real session; query ES `user_turn_ratings` index; verify all 5 carry `session_id`, `trace_id`, `prompt_callsite` |
| Future-gate | P4 eval attribution unblocked once ≥50 labeled turns exist |

### P4 — Eval Attribution (Tier-2)

| Gate | Criterion |
|------|-----------|
| Pre-merge | Eval runner (`tests/evaluation/`) updated to record `prompt_identity` per turn |
| Pre-merge | Eval report includes mean/median rating per `static_prefix_hash` bucket |
| Post-deploy | Run eval suite; report shows at least 2 `prompt_identity` buckets with per-bucket statistics |
| Future-gate | DSPy optimization pass (P6) unblocked once ≥200 rated eval turns exist |

### P5 — Agent Self-Reflection on Composition (Tier-1/2)

| Gate | Criterion |
|------|-----------|
| Pre-merge | Captain's Log reflection pipeline reads the component manifest (P1 component_ids) |
| Pre-merge | Reflection generation prompt updated to include `component_ids` and mean rating as context |
| Post-deploy | At least one Captain's Log entry references a named component (`component_id`) from the taxonomy |
| Post-deploy | A reflection that proposes a prompt change generates a proposal in the self-improvement pipeline (ADR-0058) |
| Future-gate | Composer redesign (optional phase) can proceed if P5 surfaced composition proposals |
