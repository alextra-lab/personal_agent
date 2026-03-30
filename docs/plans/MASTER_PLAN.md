# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest)
> **Source of truth for priorities**: This file
> **Last updated**: 2026-03-30

---

## Current Focus

| # | Work Item | Linear | Spec / ADR | Status |
|---|-----------|--------|------------|--------|
| 1 | Post-Slice 3 evaluation baseline established — all 4 categories passing | — | `superpowers/plans/2026-03-28-slice-3-intelligence.md` | ✅ Done |
| 2 | Post-Slice 1&2 documentation update | — | `plans/replicated-inventing-toucan.md` | In Progress |
| 3 | Qwen3.5 Model Integration | [Project 2.3](https://linear.app/frenchforest/project/23-homeostasis-and-feedback-dbce3b171536) | ADR-0023 | In Progress |

## Upcoming (approved / ready to start)

| Work Item | Linear | Spec / ADR | Depends On |
|-----------|--------|------------|------------|
| Slice 3 Stretch Goals (stability threshold, cross-session recall, proactive memory, geospatial) | — | `superpowers/plans/2026-03-28-slice-3-intelligence.md` | Post-Slice-3 baseline ✅ |
| Phase 2.3 remaining (data lifecycle, adaptive thresholds) | [Project 2.3](https://linear.app/frenchforest/project/23-homeostasis-and-feedback-dbce3b171536) | `plans/PHASE_2.3_PLAN.md` | Qwen3.5 integration |
| Phase 3.0 Daily-Use Interface | [Project 3.0](https://linear.app/frenchforest/project/30-daily-use-interface-60a517bd90f6) | — | — |

## Backlog (needs approval)

| Work Item | Linear | Spec / ADR |
|-----------|--------|------------|
| Captain's Log ES Backfill | — | `specs/CAPTAINS_LOG_ES_BACKFILL_SPEC.md` |

## Completed

| Phase | Completed | Summary |
|-------|-----------|---------|
| Context Intelligence — Phase 4 ENHANCE (implementation) | 2026-03-30 | Rolling LLM summarization (ADR-0038, compressor role), async background compression (threshold-triggered, fire-and-forget), structured context assembly (state document), KV cache prefix stability verification, cross-session eval paths (CP-30/CP-31, multi-session runner), proactive memory design (ADR-0039), recall classifier L2 design. 37 total eval paths. EVAL-10 run pending. `superpowers/plans/2026-03-30-context-intelligence.md` |
| Context Intelligence — Phase 3 VERIFY (EVAL-09) | 2026-03-30 | Full harness baseline `telemetry/evaluation/EVAL-09-post-fix-baseline/`: 34/35 paths, 176/177 assertions (99.4%). Category spot-checks: `EVAL-09-cat-context`, `EVAL-09-cat-memory`, `EVAL-09-cat-decomp-expansion`. CP-19-v3 had one assertion miss in the full run (timing); category-only run was 8/8. Meets Phase 3 gates vs EVAL-08 (`CONTEXT_INTELLIGENCE_SPEC.md`). `superpowers/plans/2026-03-30-context-intelligence.md` |
| Redesign v2 — Slice 3: Intelligence | 2026-03-29 | Enforced expansion controller, dual-mode sub-agents, per-phase time budgets, Seshat hybrid search (embeddings + vector index + fuzzy dedup), recall controller (Stage 4b), CP-19 + 6 adversarial variants passing. `superpowers/plans/2026-03-28-slice-3-intelligence.md` |
| Redesign v2 — Slice 2: Expansion | 2026-03-20 | Decomposition, sub-agents, HYBRID execution, memory promotion, Stage B delegation, insights engine. `superpowers/plans/2026-03-18-slice-2-expansion.md` |
| Redesign v2 — Slice 1: Foundation | 2026-03-19 | Pre-LLM Gateway (7 stages), single-brain architecture, MemoryProtocol, Stage A delegation. `superpowers/plans/2026-03-16-slice-1-foundation.md` |
| 2.2 Memory & Second Brain | 2026-01-23 | `plans/completed/PHASE_2.2_COMPLETE.md` |
| 2.1 Service Foundation | 2026-01-22 | `plans/completed/PHASE_2.1_COMPLETE.md` |
| 1.0 MVP (CLI Agent) | 2026-01 | 111 tests, MCP gateway (41 tools), telemetry |

---

## How This File Works

- **FIFO**: Completed items eventually drop off the bottom.
- **Linear is the task tracker**: This file tracks *priorities and sequencing*, not individual tasks.
- **Sub-plans** (e.g. `PHASE_2.3_PLAN.md`) contain implementation detail; this file links to them.
- **Specs** live in `docs/specs/`; **ADRs** in `docs/architecture_decisions/`.
- **Update cadence**: When priorities shift or phases complete.
