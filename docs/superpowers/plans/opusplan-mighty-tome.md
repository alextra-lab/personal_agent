# Prompt Management & Observability — Initiative Plan

> Status: planning (queued for ADR → LLD/spec → implementation)
> Owner-driven investigation, 2026-05-28. Architect tier (Opus) for ADR + spec.

## Context

The owner wanted to understand how LLM prompts are managed across the harness — where they
live, their size, their performance — and to test a hypothesis that prompts should be managed
centrally and injected where needed.

Investigation (3 Explore agents + Codex collaboration) found:

- Prompts are **scattered Python string constants** — no `prompts/` dir, no registry, no
  templating engine, no versioning. ~10 self-contained "leaf" prompts (entity extraction
  `second_brain/entity_extraction.py:28` ~1075 tok; context compressor
  `orchestrator/context_compressor.py:34` ~975 tok; reflection, HTML gen, router, sub-agent,
  session summary, feedback).
- The **main orchestrator prompt is not a file** — it is assembled imperatively in
  `orchestrator/executor.py:1835-2244` from ordered fragments (deployment context, operator
  stanza, skill index + matched `docs/skills/*.md` bodies, memory, tool-awareness, tool-use
  rules, decomposition). Largest effective prompt per turn; currently invisible (only 100-char
  previews logged at `executor.py:2226`).
- **Token counting is heuristic** — `words×1.3` in `request_gateway/budget.py:27` vs `chars//4`
  in `orchestrator/context_window.py:15`; no tokenizer.
- **KV-cache constraint** (ADR-0038): assembly is ordered for prefix stability, but Codex found
  the prefix is *not* actually static — tool-awareness is prepended late (`executor.py:2171`),
  memory sits mid-prefix — so cache erosion is an active, unmeasured risk.
- **Observability gap**: `llm_client/telemetry.py` captures latency/tokens/cache-read/cost per
  call, joinable by `span_id` — but **no prompt identity/version/hash**, and prompt text is
  never persisted. A third call path, `gateway/chat_api.py` (direct Anthropic, `_SYSTEM_PROMPT`
  at :39), **bypasses canonical telemetry entirely**.
- Eval harness (`tests/evaluation/harness/`, 37 paths) is **behavioral** — asserts on telemetry
  events, not prompt quality. An A/B dual-gateway rig exists (`docker-compose.eval.yml`). DSPy
  is wired (`llm_client/dspy_adapter.py`) but optimization is unused (one signature,
  `captains_log/reflection_dspy.py:76`).

### Reframe (the core decision)

The missing primitive is **prompt identity**, not a registry. "Put all prompts in one place"
helps the easy part (leaf prompts) and hides the hard part (the orchestrator prompt is a
*composition pipeline*, not a string). The cheapest, highest-leverage path is:

1. **Legibility first** (the owner's own ask: "I want to read our prompts") — a corpus renderer
   that also *names every prompt component*, producing the taxonomy everything else needs. This
   directly enables **harness compression** (dedup, cut bloat, fix persona drift, find dead
   skills).
2. **Prompt identity** — stamp every LLM call with `callsite` + ordered `component IDs` +
   `static_prefix_hash` + `dynamic_hash`, extending the existing ADR-0074 identity-tuple /
   joinability work (NOT a parallel scheme). Cover the `gateway/chat_api.py` fork.
3. **Consume** — six value loops hang off identity (see below).

### Six value loops (the "so what")

| # | Loop | Consumer | Value |
|---|------|----------|-------|
| 1 | Cost/cache attribution | owner | attribute tokens/$/cache-erosion to a prompt component |
| 2 | Cache-erosion alarm | owner + automation | metric proving the prefix is (un)stable |
| 3 | Silent-drift detection | automation | edit a skill/tool desc → hash shift → alert |
| 4 | Eval attribution | decision support | A/B rig results joinable to a prompt version |
| 5 | Agent self-reflection | Seshat | Captain's Log reflects on its own composition |
| 6 | Response quality | owner → all | per-turn 0–3 rating = ground-truth label (join key: identity) |

Loop #6 is the capstone label that #4/#5 lack; it is only useful if it carries prompt identity
(#1). Owner chose a **0–3 anchored scale**: 0=no value, 1=low, 2=meets expectation, 3=wow.

## Integration points (build on, do not duplicate)

- **ADR-0074** identity tuples / joinability probe → extend for prompt identity. See FRE-377.
- **ADR-0054** dual-write (durable file + ES + bus) → reuse for the rating capture surface.
- **ADR-0057 / FRE-247** InsightsEngine pattern (`detect_delegation_patterns`) → model the
  feedback/quality consumer on it.
- **ADR-0058 / FRE-248** self-improvement pipeline + `captain_log.entry_created` bus → loop #5.
- **ADR-0038** context compressor / `compute_prefix_hash` (`context_window.py:353`) → fix so it
  hashes the *effective* assembled system prompt, not `output_messages[0]`.
- **FRE-267 (Approved)** "per-session thumbs feedback" → **CANCEL / supersede.** Salvage its
  backend design (dual-write path `telemetry/user_feedback/`, dedicated
  `stream:user.feedback_received`, Insights consumer). New work is per-turn, 0–3, identity-bearing.
- Prior prompt work for reference: FRE-105 (prompt audit/dead-code), FRE-111 (Kibana token
  breakdown by role), FRE-274 (per-prompt token/cache capture), FRE-46/FRE-48 (token bloat).

## Artifacts to produce (this initiative)

1. **ADR-00XX — Prompt Management & Observability** (`docs/architecture_decisions/`; confirm next
   number — likely 0076 after ADR-0075/FRE-388). Decisions:
   - D1: prompt identity is the foundational primitive (extends ADR-0074); schema =
     `callsite` + ordered `component_ids` + `static_prefix_hash` + `dynamic_hash`.
   - D2: leaf prompts get stable semantic IDs + content hashes; the orchestrator prompt gets a
     named **component taxonomy** + composition contract preserving KV-cache ordering.
   - D3: legibility via a **corpus renderer** (static, source-derived) + debug-gated
     **assembled-prompt capture** (runtime); prompt text NOT persisted by default (privacy).
   - D4: telemetry stamping incl. the `gateway/chat_api.py` fork; fix `compute_prefix_hash`.
   - D5: per-turn 0–3 value rating carrying identity, superseding FRE-267.
   - D6: consumers (cost/cache attribution, drift, eval attribution, self-reflection) reuse
     existing pipelines (ADR-0054/0057/0058).
2. **LLD / Spec** (`docs/specs/PROMPT_MANAGEMENT_SPEC.md`) — component taxonomy enumeration,
   renderer output format, identity schema + where stamped, rating data model + UI placement
   (per-turn, co-located per message), consumer designs, token-count unification.
3. **Linear** (FrenchForest, label `PersonalAgent`, state **Needs Approval**, one tier label each):
   - Cancel FRE-267 with a comment linking the new epic.
   - Create an **EPIC** + phased child tickets (phasing below).
4. **Push docs** (ADR + spec) direct-to-main for iPad/GitHub review (docs = direct push;
   code = branch + PR). Update `docs/plans/MASTER_PLAN.md`.

## Phasing (dependency spine → tickets)

- **P0 — Legibility / corpus renderer** (Tier-2). Names the component taxonomy; emits a
  regenerable, token-annotated `docs/reference/PROMPT_CORPUS.md`. AC: renders every leaf prompt
  + skill doc + tool description + the composition skeleton with token counts; regenerable;
  diff is human-readable.
- **P1 — Prompt identity primitive** (Tier-1 schema / Tier-2 impl). Stamp identity on every
  call incl. gateway fork; fix `compute_prefix_hash`. AC: `model_call_completed` carries
  `prompt_callsite`, `prompt_component_ids`, `static_prefix_hash`, `dynamic_hash`; joinable in
  ES; gateway path no longer untelemetered.
- **P2 — Cost/cache attribution + drift** (Tier-2). Kibana/observations views sliced by
  identity; drift alert on prefix-hash distribution shift. AC: per-component token/$/cache view;
  alert fires on a deliberately churned prefix.
- **P3 — Per-turn 0–3 rating** (Tier-2, supersedes FRE-267). Capture endpoint (dual-write),
  per-message PWA control, identity attached. AC: rating persisted with identity tuple; bus
  event increments; PWA control per assistant message.
- **P4 — Eval attribution** (Tier-2). A/B rig + ratings as the metric. AC: eval run reports
  mean/median rating per prompt version.
- **P5 — Agent self-reflection on composition** (Tier-1/2). Captain's Log reads component
  manifest + ratings. AC: a reflection references composition; proposal flows to existing pipeline.
- **(Optional) Composer redesign** for KV-cache prefix stability (Tier-1) — gated on P2 data.

Multi-phase epic stays **In Progress** until P5 ships (never Done early). Each ticket carries a
full Acceptance Criteria table (pre-merge / post-deploy / future-gate).

## Verification

- ADR + spec render correctly and are pushed (visible on GitHub/iPad).
- FRE-267 shows Canceled with a superseding link; new epic + P0–P5 exist in Needs Approval.
- MASTER_PLAN.md header + "Last updated" reflect the new initiative.
- (Implementation verification deferred to each phase's own plan, post-approval.)
