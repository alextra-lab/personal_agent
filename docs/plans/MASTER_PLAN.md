# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest)
> **Source of truth for priorities**: This file
> **Last updated**: 2026-03-21

---

## Current Focus

| # | Work Item | Linear | Spec / ADR | Status |
|---|-----------|--------|------------|--------|
| 1 | Evaluation & data collection — building real usage traces | — | `specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` | In Progress |
| 2 | Post-Slice 1&2 documentation update | — | `plans/replicated-inventing-toucan.md` | In Progress |
| 3 | Qwen3.5 Model Integration | [Project 2.3](https://linear.app/frenchforest/project/23-homeostasis-and-feedback-dbce3b171536) | ADR-0023 | In Progress |

## Upcoming (approved / ready to start)

| Work Item | Linear | Spec / ADR | Depends On |
|-----------|--------|------------|------------|
| Slice 3: Intelligence (proactive memory, programmatic delegation, self-improvement) | — | `specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` Section 8.3 | Evaluation data from current usage phase |
| Phase 2.3 remaining (data lifecycle, adaptive thresholds) | [Project 2.3](https://linear.app/frenchforest/project/23-homeostasis-and-feedback-dbce3b171536) | `plans/PHASE_2.3_PLAN.md` | Qwen3.5 integration |

### Pre-Slice 3 Design Constraints (from Evaluation Phase)

**EVAL-03 critical finding** (`docs/research/EVAL_03_MEMORY_PROMOTION_REPORT.md`):

The episodic→semantic promotion stability score prevents organic promotion:

```python
score = min(mention_count / 100.0, 0.5) + min(days_span / 90.0, 0.5)
```

An entity needs **50 mentions** or **90 days** of spread to reach a meaningful score. In 456 captures over 5 days, no entity promotes organically. The current 990 semantic entities were force-promoted for evaluation.

**Required before Slice 3 proactive memory feature can work:**
- Redesign the promotion threshold — options: recency boost, relative top-N, or lower `min_mentions` to 3–5
- Validate cross-session recall (entities seeded in session A recalled in session B via Neo4j query, not session history)

**What's working well:**
- Entity extraction (gpt-4.1-nano): ~100% accuracy across people, projects, technologies, decisions
- Session-scoped memory recall: 100% across 5 diverse scenarios
- Promotion pipeline mechanics: wired and functional (FRE-148)

## Backlog (needs approval)

| Work Item | Linear | Spec / ADR |
|-----------|--------|------------|
| Phase 3.0 Daily-Use Interface | [Project 3.0](https://linear.app/frenchforest/project/30-daily-use-interface-60a517bd90f6) | — |
| Captain's Log ES Backfill | — | `specs/CAPTAINS_LOG_ES_BACKFILL_SPEC.md` |

## Completed

| Phase | Completed | Summary |
|-------|-----------|---------|
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
