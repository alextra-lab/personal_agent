# Context Intelligence — Design Specification

> **Status:** Proposed
> **Date:** 2026-03-30
> **Author:** Project owner + Cursor (brainstorming session)
> **Depends on:** EVAL-08 baseline (b6e22da), Slice 3 complete
> **Research input:** `docs/research/context_management_research.md` (moved from root)
> **Supersedes:** Slice 3 Stretch Goals section of `docs/research/EVAL_08_SLICE_3_PRIORITIES.md`

---

## Purpose

Deliver the next evolution of the agent's cognitive architecture — from passive context truncation to active context management. This spec covers four sequential phases: documentation cleanup, targeted fixes for EVAL-08 failures, rigorous verification, and new capabilities drawn from context management research.

The serial dependency chain is the defining constraint: each phase's output is the next phase's input. No phase begins until the previous phase's exit criteria are met.

## Evolutionary Context

The agent has passed through three architectural slices:

| Slice | Theme | Biological Analogy |
|-------|-------|--------------------|
| Slice 1: Foundation | One brain, clean interface | Prokaryotic — basic cell structure, single organism |
| Slice 2: Expansion | Learn to breathe — expand/contract | Eukaryotic — internal organelles (sub-agents, memory) |
| Slice 3: Intelligence | Gets smarter about itself | Colonial — cells cooperating, some coordination |

This spec moves the agent toward **differentiated** — specialized systems managing their own domains. The context management system becomes an active cognitive function rather than a buffer overflow handler.

## Scope

**In scope:**
- Documentation accuracy and navigation (Phase 1)
- EVAL-08 failure fixes: recall controller, entity promotion, expansion events (Phase 2)
- Targeted and full evaluation runs proving fixes work (Phase 3)
- New context management capabilities: summarization, compression, proactive memory, recall classifier (Phase 4)

**Out of scope:**
- Product UX or interface changes (Phase 3.0 Daily-Use Interface is separate)
- Unrelated refactoring of code not touched by these phases
- Decomposition learning / threshold tuning (deferred per EVAL-08 priorities §Rank 8)
- Full procedural memory system (deferred — insufficient data)
- Self-improvement loop closure (deferred — prerequisite chain unmet)

## Research Sources

This spec integrates findings from three sources:

1. **EVAL-08 Post-Slice-3 Baseline** (`telemetry/evaluation/EVAL-08-new-local-baseline/`) — 18/35 paths, 77.2% assertions. Context Management at 12%, Memory Quality at 0%.
2. **Context Management Research** (`docs/research/context_management_research.md`) — Perplexity + GPT-5.4 discussion on context window management, async compression, and memory recall detection.
3. **Slice 3 Stretch Goals** (`docs/research/EVAL_08_SLICE_3_PRIORITIES.md` §Stretch) — Proactive memory, stability threshold redesign, cross-session recall validation.

---

## Phase Structure

```
Phase 1: CLEAN          Phase 2: FIX           Phase 3: VERIFY         Phase 4: ENHANCE
─────────────────────  ─────────────────────  ─────────────────────  ─────────────────────
Accurate docs          Code fixes for         Run evals, prove       New capabilities
that reflect           EVAL-08 failures       fixes actually work    from research
post-Slice-3 reality                          (or loop back)

     │                      │                      │
     ▼                      ▼                      ▼
Fixes built on         Verification tests     Enhancements built
accurate context       the right things       on verified baseline
```

### Phase Gates

| Gate | Exit Criteria | Rationale |
|------|--------------|-----------|
| 1→2 | All spec statuses match reality. No agent can land on a doc that contradicts the post-Slice-3 state. README/AGENTS.md files route correctly. | Phase 2 agents need accurate context to fix the right things. |
| 2→3 | All targeted code changes merged. Recall controller handles all 7 CP-19 variants. Promotion pipeline diagnosed and fixed. Expansion events emit. All existing tests pass. | Can't verify what isn't fixed. |
| 3→4 | EVAL-09 proves measurable improvement: Context Management >=60% (was 12%), Memory Quality >=50% (was 0%), overall assertions >=86% (was 77.2%). If targets not met, loop back to Phase 2 with evidence. | No claiming victory without numbers. |
| 4→Done | Enhancement features pass targeted evals. EVAL-10 shows no regression from EVAL-09 baseline. | Evolution, not mutation. |

### Loop-Back Rule

Phase 3 can send work back to Phase 2. When it does, it provides:
- The specific assertion that failed
- The trace_id from the eval
- The pipeline walk showing where the failure occurred (not "it didn't work" but "Stage 4b was skipped because X")

Max 3 fix-verify cycles per category before escalating to the project owner.

---

## Phase 1: CLEAN (Documentation Triage)

**Goal:** Reduce the 205-file `docs/` corpus to a navigable, accurate knowledge base.

**Model assignment:** Sonnet (fast). Mechanical markdown work.

**Principle:** Archive and compress. Don't just move files — extract useful content into condensed references, then remove the verbose originals. A single `docs/archive/PRE_REDESIGN_SUMMARY.md` is worth more than 15 v0.1 files.

### 1.1 — Archive and Compress Superseded Documents

Move superseded content to `docs/archive/` with consolidation:

**Architecture v0.1 docs (~15 files):** Compress into `docs/archive/PRE_REDESIGN_SUMMARY.md` capturing key decisions and evolution. Then archive individual files.

| Source | Estimated Files | Disposition |
|--------|----------------|-------------|
| `docs/architecture/*_v0.1.md` (4 already have SUPERSEDED banners) | ~15 | Compress key decisions → archive |
| `docs/architecture/experiments/` (router experiments) | 3 | Archive — router removed |
| `docs/architecture_decisions/experiments/` (old experiment proposals) | ~10 | Archive — tied to old architecture |
| `docs/architecture_decisions/` non-ADRs: `PROJECT_STATUS_2025-12-28.md`, `RTM.md`, `METRICS_FORMAT_PROPOSAL.md` | 3 | Archive — phase 1.0-2.2 snapshots |
| `docs/plans/sessions/` pre-March-2026 | ~8 | Archive — historical |
| `docs/research/` router-era research | ~4 | Extract useful findings → archive |
| `docs/plans/DOCS_REORG_AND_WORKFLOW_PLAN.md` | 1 | Archive — marked COMPLETE |

**Keep (hot docs):**
- All ADRs (0001-0037) — decision record, never archive
- `COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` — canonical architecture
- `MASTER_PLAN.md`, `VISION_DOC.md` — canonical priorities and philosophy
- All `docs/research/EVAL_*` files — active evaluation data
- All `docs/guides/` — usage docs (updated in 1.2)
- `HOMEOSTASIS_MODEL.md`, `HUMAN_SYSTEMS_MAPPING.md` — living conceptual docs

### 1.2 — Fix Accuracy in Hot Documents

| Document | Issue | Fix |
|----------|-------|-----|
| `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` | Header says Slice 3 "Planned" | Update to "Complete (2026-03-29)" |
| `docs/specs/CONVERSATION_CONTINUITY_SPEC.md` | Status "Proposed" but largely implemented | Update to "Partially Implemented" |
| `docs/specs/SEARXNG_WEB_SEARCH_TOOL_SPEC.md` | Status "Proposed" but implemented | Update to "Implemented" |
| `docs/architecture/README.md` | Says Slice 3 "Planned" | Update for Slice 3 complete |
| `docs/architecture_decisions/GOVERNANCE_MODEL.md` | Empty file (0 bytes) | Populate or delete |
| `docs/architecture_decisions/HYPOTHESIS_LOG.md` | References Planner+Critic | Update for Redesign v2 |
| `docs/architecture_decisions/TECHNICAL_DEBT.md` | References old infra | Refresh for current state |
| `docs/guides/SLM_SERVER_INTEGRATION.md` | Diagram shows multi-tier router | Update for single-brain + gateway |
| `docs/guides/MCP_INTEGRATION_QUICK_START.md` | Reads like migration log | Rewrite as actual quick start |
| `docs/reference/PROJECT_DIRECTORY_STRUCTURE.md` | Last updated Dec 2025 | Regenerate from current tree |
| `docs/reference/CODING_CONVENTIONS.md` | Overlaps CODING_STANDARDS.md | Merge into CODING_STANDARDS, archive original |
| `docs/research/README.md` | Links to router-era docs | Update for current focus |
| `docs/architecture_decisions/EXPERIMENTS_ROADMAP.md` | Centers on three-stage routing | Update for Redesign v2 |

### 1.3 — Update Navigation Aids

- Update `docs/architecture/README.md` as canonical "start here" with accurate reading order
- Add `AGENTS.md` to `docs/specs/`, `docs/research/`, `docs/guides/`
- Update VISION_DOC.md "For New AI Assistants" if reading-order paths changed

### Phase 1 Exit Criteria

- [ ] Zero documents with status fields that contradict reality
- [ ] All superseded docs archived with consolidated summaries
- [ ] `PROJECT_DIRECTORY_STRUCTURE.md` matches actual tree
- [ ] Agent navigating VISION_DOC → README → MASTER_PLAN lands on accurate info at every step
- [ ] Net active doc count reduced >=30% from baseline (206 files in `docs/**/*.md` as of 2026-03-30)
- [ ] `ContextManagement_checkin.md` moved from repo root to `docs/research/context_management_research.md`

---

## Phase 2: FIX (EVAL-08 Failures)

**Goal:** Fix the three failure categories dragging EVAL-08 scores.

**Model assignment:** Sonnet for all sub-tasks. Opus only if diagnosis reveals architectural issues.

**History:** These issues have been identified in prior evals (EVAL-03, EVAL-04) and partially addressed in Slice 3. The recall controller was built for CP-19 but misses adversarial variants. The promotion threshold was flagged in EVAL-03. Expansion events were noted in EVAL-04. Phase 2 exists because understanding a fix is not the same as delivering it.

### 2.1 — Recall Controller Pattern Expansion

**Targets:** CP-19-v2 through CP-19-v7 (6 of 7 Context Management failures)

**Critical insight:** Several failing cue phrases ("remind me what", "refresh my memory", "what did we decide", "the X we discussed") appear to match existing patterns in `_RECALL_CUE_PATTERNS`. Before adding patterns, the first step is to run unit tests against the actual inputs to determine whether the bug is in the regex or in the pipeline (event emission, gate logic, Stage 4b invocation conditions).

**Approach:**
1. Write unit tests for `_RECALL_CUE_PATTERNS` with all 7 CP-19 variant inputs
2. Run to find which fail to match vs which match but don't trigger the event
3. Fix regex where regex is the issue
4. Fix pipeline/event emission where patterns match but controller doesn't fire
5. Run full recall controller test suite

**Files:**
- Modify: `src/personal_agent/request_gateway/recall_controller.py`
- Test: `tests/personal_agent/request_gateway/test_recall_controller.py`

### 2.2 — Entity Promotion Pipeline Diagnosis

**Targets:** CP-26, CP-27 (Memory Quality 0%)

**The actual problem:** The `min_mentions` threshold is already 1 (`consolidator.py` line 166). The promotion pipeline's real bottleneck is the `stability_score()` formula in `src/personal_agent/memory/fact.py`:

```python
mention_factor = min(self.mention_count / 100.0, 0.5)  # 100 mentions for max
time_factor = min(days / 90.0, 0.5)                     # 90 days for max
```

An entity mentioned 3 times in a single eval session gets a stability score of ~0.03. The prior "50-mention" diagnosis was a shorthand for this formula — entities need extreme counts or time spans to score meaningfully.

**But the pipeline calls `promote_entity()` regardless of score** — no minimum confidence gate exists in `promote.py`. So the question is: **why are entities not appearing in Neo4j at all?** Possible causes:
1. Entity extraction not running during eval conversations
2. `consolidator.py` not triggered between turns
3. Neo4j writes succeeding but with different entity names than the eval queries
4. `memory_enrichment_completed` event not emitting (confirmed missing in CP-26, CP-27)

**Approach (diagnosis first):**
1. Trace a CP-26-style conversation with debug logging
2. Verify entity extraction fires during the session
3. Verify consolidator runs and calls `get_promotion_candidates()`
4. Verify `promote_entity()` succeeds and entities are queryable
5. Fix the actual failure point (may be timing, may be extraction, may be the stability formula)
6. If stability score is the gate: redesign with recency boost so recent entities promote faster

**Files:**
- Diagnose: `src/personal_agent/memory/fact.py` (stability_score)
- Diagnose: `src/personal_agent/second_brain/consolidator.py` (promotion trigger)
- Diagnose: `src/personal_agent/memory/promote.py` (promotion execution)
- Diagnose: `src/personal_agent/memory/service.py` (promote_entity)
- Diagnose: `src/personal_agent/second_brain/entity_extraction.py` (extraction trigger)
- Test: Existing promotion tests, updated for identified root cause

### 2.3 — Expansion Event Telemetry

**Targets:** CP-09, CP-10, CP-11, CP-16, CP-17 (Decomposition + Expansion failures)

**The problem:** Gateway correctly classifies HYBRID/DECOMPOSE. `planner_started` and `expansion_dispatch_started` fire. But `hybrid_expansion_start` and `hybrid_expansion_complete` do not.

**Diagnosis first:**
1. Grep for `hybrid_expansion_start` — where should it be emitted?
2. Compare against what expansion controller actually emits
3. If naming mismatch: fix code or eval assertions (whichever is wrong)
4. If pipeline bug: trace through expansion controller

**Files:**
- Potentially: `src/personal_agent/orchestrator/expansion_controller.py`
- Potentially: Eval scenario definitions
- Test: Category eval run for expansion

### CP-28 and CP-29 Disposition

**CP-28** (Context Budget Trimming Audit): Fails on `memory_recall` classification at turn 10 after 8 turns of context. This is a recall controller issue (same root cause as CP-19 variants) combined with a budget trimming interaction. Phase 2.1 may partially fix it. If not, Phase 3 diagnosis will identify whether it's a recall pattern gap or a budget trimmer evicting critical facts.

**CP-29** (Delegation Package Completeness): Fails on delegation intent classification ("Use Claude Code to..." classified as conversational). This is outside Phase 2's scope — it's an intent classification issue, not a recall/promotion/expansion issue. Accepted as a known failure for EVAL-09; tracked separately if prioritized later.

### Phase 2 Exit Criteria

- [ ] Unit tests pass for all 7 CP-19 variant recall cue inputs
- [ ] Entity promotion pipeline diagnosed — root cause identified and fixed
- [ ] `hybrid_expansion_start/complete` events appear in telemetry
- [ ] All existing tests pass (`uv run pytest`)
- [ ] No regressions in passing categories

---

## Phase 3: VERIFY (Prove It Works)

**Goal:** Run targeted evaluations proving Phase 2 fixes resolved EVAL-08 failures. If failures persist, loop back with evidence.

**Model assignment:** Sonnet for eval execution and analysis. Opus for persistent-failure root cause analysis.

### 3.1 — Targeted Category Evals

Run each failing category independently before a full run:

| Run | Category | EVAL-08 Baseline | Target |
|-----|----------|------------------|--------|
| EVAL-09-cat-context | Context Management (8 paths) | 1/8 (12%) | >=6/8 (75%) |
| EVAL-09-cat-memory | Memory Quality (4 paths) | 0/4 (0%) | >=2/4 (50%) |
| EVAL-09-cat-expansion | Decomposition + Expansion (7 paths) | 2/7 (29%) | >=5/7 (71%) |

Targets are not 100% because some failures may have root causes beyond Phase 2's scope (e.g., CP-29 delegation intent classification).

### 3.2 — Persistent Failure Diagnosis Protocol

If a category still fails after Phase 2 fixes:

1. Read the specific assertion failure from eval results
2. Pull the trace from Elasticsearch using the trace_id
3. Walk the pipeline — did the recall controller fire? Did the event emit? Did the gateway classify correctly?
4. Identify the actual failure point with file/line reference
5. Document finding as a specific bug
6. Loop back to Phase 2 with that evidence

Max 3 fix-verify cycles per category. Escalate to project owner after 3.

### 3.3 — Full Baseline Run (EVAL-09)

After category evals meet targets, run the full 35-path harness:

| Metric | EVAL-08 | EVAL-09 Target |
|--------|---------|----------------|
| Paths Passed | 18/35 (51%) | >=25/35 (71%) |
| Assertions Passed | 139/180 (77.2%) | >=155/180 (86%) |
| Context Management | 1/8 (12%) | >=6/8 (75%) |
| Memory Quality | 0/4 (0%) | >=2/4 (50%) |
| Decomposition | 1/4 (25%) | >=3/4 (75%) |
| Expansion & Sub-Agents | 1/3 (33%) | >=2/3 (67%) |
| Intent (maintain) | 6/7 (86%) | >=6/7 |
| Memory System (maintain) | 4/4 (100%) | 4/4 |
| Tools (maintain) | 3/3 (100%) | 3/3 |
| Edge Cases (maintain) | 2/2 (100%) | 2/2 |

Save as: `telemetry/evaluation/EVAL-09-post-fix-baseline/`

**EVAL-09 assumptions:** Same harness, same scenarios, same infrastructure as EVAL-08. The 25/35 path target assumes CP-05 (delegation timeout at 300s) and CP-29 (delegation classification) remain unchanged — these are outside Phase 2 scope.

**EVAL-10 comparison method:** Same harness run after Phase 4 enhancements. Compare assertion pass rates per category against EVAL-09 baseline. New eval paths (CP-30+ for cross-session recall) are additive — they don't change EVAL-09 category baselines.

### Phase 3 Exit Criteria

- [ ] EVAL-09 committed with results
- [ ] Overall assertions >=86%
- [ ] No regression in previously-passing categories
- [ ] All persistent failures documented with root-cause analysis
- [ ] MASTER_PLAN updated with EVAL-09 results

---

## Phase 4: ENHANCE (Context Intelligence)

**Goal:** Build new capabilities from context management research on top of the verified EVAL-09 baseline.

**Model assignments:** See per-task table below.

### Core Sub-Tasks

| # | Sub-Task | Source | Effort | Model |
|---|----------|--------|--------|-------|
| 4.1 | Rolling LLM Summarization | Research Strategy 2 + CONVERSATION_CONTINUITY_SPEC deferred work | M | Sonnet |
| 4.2 | Async Background Compression | Research Technique 1 | S | Sonnet |
| 4.3 | Proactive Memory (`suggest_relevant()`) | Slice 3 stretch goal | L | Opus (design) / Sonnet (impl) |
| 4.4 | Cross-Session Recall Validation | Slice 3 stretch goal | S | Sonnet |
| 4.5 | Structured Context Assembly | Research Strategy 4 | M | Sonnet |
| 4.6 | KV Cache Preservation (stable prefix) | Research Technique 4 | S | Sonnet |
| 4.7 | Recall Classifier Layer 2 + Intent-Aware Recall | Research Sections 4 + 9B | M | Opus (design) / Sonnet (impl) |

### Stretch Sub-Tasks

| # | Sub-Task | Source | Effort | Model |
|---|----------|--------|--------|-------|
| 4.S1 | LLM-as-Judge (Recall Layer 3) | Research Section 5 | S | Sonnet |
| 4.S2 | Context Gap Score | Research Section 9A | M | Opus (design) |

### Dependency Order

```
4.1 (summarization) → 4.2 (async) → 4.5 (structured assembly) → 4.6 (KV cache)
                                          ↑
4.3 (proactive memory) ─────────────────────┘
4.4 (cross-session) — independent
4.7 (classifier) — independent, after Phase 3 verifies regex fixes
Stretch — after core items complete
```

### 4.1 — Rolling LLM Summarization

Replace `[Earlier messages truncated]` in `context_window.py` with compressed summaries. When `apply_context_window()` evicts turns, pass the evicted span to a compressor model that generates a structured summary (key facts, decisions, entities). Add `compressor` role to `models.yaml` pointing to a small/fast model.

### 4.2 — Async Background Compression

Run summarization as `asyncio.create_task()` between turns. Fire when token count crosses 65% of context window. Summary ready before Turn N+2 needs it.

### 4.3 — Proactive Memory

Seshat injects cross-session context during `assemble_context()` without being asked. Design decisions needed: relevance scoring, noise control, token budget allocation, A/B validation methodology.

**Prerequisite:** Phase 2's promotion threshold fix (entities must exist in Neo4j) verified in Phase 3.

### 4.4 — Cross-Session Recall Validation

New eval paths (CP-30+) that seed entities in one session, close it, open a new session, and query those entities. This has never been tested — EVAL-08's 100% Memory System score is entirely within-session.

### 4.5 — Structured Context Assembly

Redesign `assemble_context` to prepend a living state document each turn:

| Section | Content |
|---------|---------|
| Goal | Current task and clarifications |
| Constraints | Decisions locked in, tech stack |
| State | What's been done, artifacts created |
| Open Questions | Unresolved decisions |
| Recent Actions | Last 3-5 steps |

### 4.6 — KV Cache Preservation

Keep system prompt + anchor summary immutable per session until a compression event. Only the suffix (recent turns) changes each turn. Provider-dependent benefit — estimated 30-50% latency reduction on cached providers.

### 4.7 — Recall Classifier Layer 2

Lightweight classifier for implicit references regex can't catch ("Can we refine it?", unresolved anaphora). Embedding similarity between current input and recent turns + semantic completeness scoring. Intent-aware filtering — only trigger for troubleshooting/refinement/continuation intents, not general knowledge.

### Phase 4 Exit Criteria

- [ ] Rolling summarization operational — evicted turns become summaries, not silence
- [ ] Compressor model configured in `models.yaml`
- [ ] Async compression fires on threshold (verified in telemetry)
- [ ] Cross-session recall tested with new eval paths
- [ ] Proactive memory has design doc + initial implementation (or documented deferral with evidence)
- [ ] EVAL-10 full run shows no regression from EVAL-09
- [ ] Relevant ADRs created for architectural decisions
- [ ] MASTER_PLAN updated

---

## Model Assignment Summary

| Task Type | Model | Rationale |
|-----------|-------|-----------|
| Doc archiving, banner-adding, markdown edits | Sonnet (fast) | Mechanical, no reasoning |
| Spec status updates, README rewrites | Sonnet (fast) | Straightforward accuracy |
| Recall controller regex + unit tests | Sonnet | Well-scoped pattern matching |
| Promotion threshold change | Sonnet | Config + small code change |
| Expansion event diagnosis | Sonnet | Grep + trace analysis |
| Eval runs + result analysis | Sonnet | Execution + comparison |
| Root-cause analysis of persistent failures | Opus | Deep multi-file reasoning |
| Rolling summarization implementation | Sonnet | Well-specified from research |
| Proactive memory design | Opus | Architectural decisions |
| Recall classifier design | Opus | Architectural decisions |
| Cross-session eval path design | Sonnet | Follows existing harness |
| Spec writing | Opus | Captures nuance and dependencies |
| Plan writing (bite-sized tasks) | Sonnet | Follows template |

**Cost principle:** Sonnet by default. Opus only for work requiring complex multi-file context or architectural tradeoff decisions.

---

## Linear Issue Structure

All issues created with `state: "Needs Approval"` and `labels: ["Needs Approval", "PersonalAgent"]`.

| Phase | Project | Issues |
|-------|---------|--------|
| Phase 1 | Documentation Triage | 1.1 Archive & Compress, 1.2 Fix Accuracy, 1.3 Navigation Aids |
| Phase 2 | EVAL-08 Fixes | 2.1 Recall Controller Patterns, 2.2 Promotion Threshold, 2.3 Expansion Events |
| Phase 3 | EVAL-09 Verification | 3.1 Category Evals, 3.2 Persistent Failure Diagnosis (if needed), 3.3 Full Baseline |
| Phase 4 | Context Intelligence | 4.1-4.7 per sub-task, stretch goals as separate issues |

Each issue description must link to this spec and relevant ADRs per workspace policy.

---

## Acceptance Criteria (Full Spec)

- [ ] Phase 1: Active docs corpus reduced >=30%, zero accuracy contradictions
- [ ] Phase 2: All targeted EVAL-08 fixes merged and unit-tested
- [ ] Phase 3: EVAL-09 overall >=86% assertions, no regressions
- [ ] Phase 4: Rolling summarization + async compression operational
- [ ] Phase 4: At least one cross-session recall eval path passing
- [ ] Phase 4: EVAL-10 no regression from EVAL-09
- [ ] All phases: MASTER_PLAN updated after each phase completion
- [ ] All phases: Relevant ADRs created or updated

---

## Open Questions

1. **Compressor model choice:** Which model for the `compressor` role? Candidates: small local model (Qwen-3B quantized), Haiku, or the existing primary model at lower temperature. Tradeoff: quality vs latency vs cost.
2. **Proactive memory noise:** Does injecting cross-session context help or hurt? Requires A/B testing. May need a "proactive memory budget" (max tokens allocated).
3. **Cross-session eval design:** How to handle Neo4j state between eval sessions? Need a clean/seed/query pattern that doesn't pollute the graph.
4. **Context Gap Score feasibility:** Requires reliable entity extraction on the current turn to compute `required_entities - provided_entities`. Current NP extraction in recall controller is heuristic. Worth building?

---

*This spec converts the EVAL-08 baseline findings, Slice 3 stretch goals, and context management research into an actionable 4-phase plan with serial dependencies, measurable gates, and model-appropriate task assignment.*
