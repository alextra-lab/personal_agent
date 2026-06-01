# ADR-0082 — Tier-Aware Model Selection for SINGLE-Strategy Tasks

**Status:** Proposed — 2026-06-01
**Related:** ADR-0033 (multi-provider model taxonomy — defines the `primary` / `sub_agent` tiers), ADR-0063 (primitive tools & action-boundary governance — owns the gateway decomposition matrix), ADR-0023 (thinking-budget control), ADR-0078/0081 (prompt/cache work — the cost context this sits beside), FRE-407 (per-turn quality rating — the quality guardrail)

---

## Context

The gateway's Stage 5 decomposition matrix (`request_gateway/decomposition.py`) decides **how a task is shaped** — `SINGLE` vs `HYBRID`/`DECOMPOSE`/`DELEGATE`. But it does **not** decide **which model tier runs a `SINGLE` task**. Every `SINGLE` turn is pinned to the primary model by `_determine_initial_model_role()` (`executor.py:974`), which unconditionally returns `ModelRole.PRIMARY`. There is no tier-selection axis at all.

Two tiers exist and are deliberately differentiated (ADR-0033, `config/models.yaml`):

| Tier | Model | Thinking | Context | Concurrency | Timeout | Built for |
|------|-------|----------|---------|-------------|---------|-----------|
| `primary` | Qwen3.6-35B-A3B (thinking) | **on**, 32 768-tok budget | 131 072 | **1** (GPU-bound) | 600 s | deep reasoning, tool calling, decomposition planning |
| `sub_agent` | Qwen3.6-35B-A3B-subagent | **off** (`disable_thinking: true`) | 32 768 | **3** | 90 s | *"Instruct (Non-Thinking) Mode"* — focused single-task completion |

The `sub_agent` tier is the **non-thinking instruct** model, with native function-calling (`supports_function_calling: true`). It is currently reachable **only** through the expansion paths (HYBRID/DECOMPOSE/DELEGATE) — i.e. only when the matrix decides to fan out.

**Measured traffic (ES `agent-logs-*`, `decomposition_assessed`, 30 days, n = 2 614):**

| Strategy | Share | | Reason | Share |
|----------|-------|---|--------|-------|
| **SINGLE** | **95.0 %** | | `conversational_always_single` | **66.3 %** |
| delegate | 2.3 % | | `tool_use_single` | **16.4 %** |
| hybrid | 1.9 % | | `memory_recall_always_single` | 6.1 % |
| decompose | 0.8 % | | `analysis_simple` / `planning_simple` | ~5.9 % |

So **~83 % of all turns (`conversational` 66 % + `tool_use` 16 %) run on the thinking model regardless of how trivial they are.** A "what's the weather"-class conversational turn is served by a 35B model with a 32 768-token *thinking* budget, a 600 s timeout, and — critically — the **single** GPU inference slot, while the purpose-built non-thinking instruct tier (3× concurrency, 90 s) sits idle unless an expansion fires.

This is a cost, latency, and throughput gap:
- **Cost/latency:** trivial turns pay thinking-model prefill + thinking-token generation.
- **Throughput / head-of-line blocking:** `primary` is `max_concurrency: 1`. Every conversational turn occupies the one thinking slot, serializing behind it any turn that genuinely needs reasoning.

The decomposition matrix answers *"does this need to be broken up?"* It never answers *"does this need the thinking model at all?"* — which is the question 83 % of traffic is silently defaulting to "yes."

---

## Decision

Introduce a **model-tier selection axis** for `SINGLE`-strategy tasks: a deterministic mapping, evaluated in the gateway (alongside Stage 5), that selects `primary` vs `sub_agent` for the initial model role — independent of, and orthogonal to, the decompose/expand decision. `_determine_initial_model_role()` stops being a constant and becomes a function of the gateway's task classification.

This does **not** change expansion behavior (HYBRID/DECOMPOSE/DELEGATE keep their semantics and already use `sub_agent` workers). It adds the missing tier choice for the 95 % that stay `SINGLE`.

### D1 — Tier selection lives in the gateway, deterministically

The tier decision is made from the signals Stage 4 (intent → `TaskType`) and the decomposition assessment (`Complexity`) already produce, and is carried on `GatewayOutput` as a new `model_tier` field (mirroring how `decomposition.strategy` is carried). `_determine_initial_model_role()` reads it instead of hard-returning `PRIMARY`. No new model call — reuses existing classification. Deterministic and reviewable, consistent with the matrix it sits beside (ADR-0063 §D-series).

### D2 — Proposed tier mapping (starting point — to ratify, see Open decisions)

| TaskType | Complexity | Tier | Rationale |
|----------|-----------|------|-----------|
| `CONVERSATIONAL` | any | **`sub_agent`** | chat/social — no reasoning or tools needed |
| `MEMORY_RECALL` | any | **`sub_agent`** | retrieval + restatement; thinking adds latency, not quality |
| `TOOL_USE` | simple | **`sub_agent`** | `sub_agent` has native function-calling; single-shot tool calls don't need thinking |
| `ANALYSIS` / `PLANNING` | simple | `primary` *(conservative default)* | borderline; keep on thinking tier until eval says otherwise |
| `ANALYSIS` / `PLANNING` | moderate+ | (already HYBRID/DECOMPOSE) | unchanged |
| `SELF_IMPROVE` | any | `primary` | reflection quality matters |

This is the **conservative** cut: it moves the two unambiguous non-thinking classes (`CONVERSATIONAL`, `MEMORY_RECALL`) plus simple `TOOL_USE` — roughly **the 83 %** — off the thinking tier, and leaves everything with reasoning value on `primary`. The exact boundaries are the open decision; the mapping is data-gated on the FRE-407 quality trace, not asserted.

### D3 — Escalation path (instruct → thinking)

`sub_agent` selection must be **reversible within the turn**, not a trap. Reuse the existing role-escalation muscle: if a `sub_agent`-routed turn (a) hits its tool-iteration limit without converging, (b) errors/times out, or (c) the model itself signals it needs to reason, escalate the *same* turn to `primary` and re-run. The instruct tier is the optimistic default; the thinking tier is the fallback. This bounds the worst case to "instruct attempt + primary attempt" and prevents quality regressions from a misclassification.

### D4 — Concurrency dividend

Routing the conversational/recall bulk to `sub_agent` (`max_concurrency: 3`) **unblocks the single `primary` slot** for turns that actually reason. This is a throughput win independent of the per-turn cost saving: fewer trivial turns queued head-of-line in front of reasoning turns. (Note the local SLM server's concurrency limits — ADR-0033, models.yaml — govern how much of this is realizable on the single-GPU host vs a cloud profile.)

---

## Open decisions (data-gated)

- **The mapping boundaries (D2).** Which `TaskType × Complexity` cells route to `sub_agent`. The conservative cut above is a proposal; the labeled signal is the FRE-407 per-turn rating trace, A/B'd by tier. Candidate cells to probe: simple `ANALYSIS`/`PLANNING` (do they survive on instruct?), and whether `TOOL_USE` *moderate* can also go instruct.
- **Escalation triggers (D3).** Exact conditions for instruct→primary re-run, and whether a re-run reuses or discards the instruct partial. Tradeoff: aggressive escalation protects quality but erodes the cost win.
- **Quality floor.** The non-negotiable: mean per-turn rating for any tier-routed class must not regress vs the all-primary baseline. Define the regression threshold before rollout.
- **Interaction with skill routing.** With `prefer_primitives_enabled=ON`, `sub_agent` turns inherit the skill index (executor.py:2423-2432). Confirm the instruct tier uses `read_skill` / nudges as well as primary does, or the cost win trades against a capability loss.
- **Rollout gating.** Flag-gated, class-by-class (start with `CONVERSATIONAL` only), measured on FRE-407 before widening — never a big-bang switch on a hot path.

---

## Consequences

**Positive**
- ~83 % of turns become eligible for the cheaper, faster, non-thinking tier — the dominant cost/latency saving available on `SINGLE` traffic.
- Frees the single `primary` GPU slot for turns that actually reason (throughput / head-of-line win).
- Uses the `sub_agent` tier for exactly what ADR-0033 built it for; closes the "instruct model only reachable via expansion" gap.
- Deterministic, reviewable, no new model call (D1).

**Negative / tradeoffs**
- Misclassification risk: a reasoning task mis-tagged `CONVERSATIONAL` lands on the instruct tier. Mitigated by D3 escalation + the conservative D2 cut.
- Escalation re-runs cost a second inference on the worst case — must stay rare or the win inverts.
- Adds a routing axis to a hot path: must be flag-gated and measured (FRE-407), like every other change in this cost line.
- The concurrency dividend is bounded by the single-GPU host; fully realized only on a multi-slot / cloud profile.

---

## Verification

- **Cost/latency:** mean turn latency and token cost for `CONVERSATIONAL` / `MEMORY_RECALL` drop materially after routing to `sub_agent`; primary-slot queue depth falls.
- **Quality (gate):** FRE-407 per-turn rating for each tier-routed class is **flat-or-up** vs the all-primary baseline. Any regression past the defined floor reverts that class.
- **Escalation rate:** instruct→primary re-run rate stays below a set ceiling (else the mapping is too aggressive — retune D2).
- **No expansion regression:** HYBRID/DECOMPOSE/DELEGATE rates and outcomes unchanged (this ADR touches only the `SINGLE` tier choice).
