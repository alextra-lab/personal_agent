# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest)
> **Source of truth for priorities**: This file
> **Last updated**: 2026-03-09

---

## Current Focus

| # | Work Item | Linear | Spec / ADR | Status |
|---|-----------|--------|------------|--------|
| 1 | Test suite to ~100% pass rate | [FRE-104](https://linear.app/frenchforest/issue/FRE-104) | — | Needs Approval (prerequisite for verification hooks) |
| 2 | Qwen3.5 Model Integration | [Project 2.3](https://linear.app/frenchforest/project/23-homeostasis-and-feedback-dbce3b171536) | ADR-0023 | In Progress |
| 3 | Docs reorg & agent workflow | — | `plans/DOCS_REORG_AND_WORKFLOW_PLAN.md` | In Progress |

## Upcoming (approved / ready to start)

| Work Item | Linear | Spec / ADR | Depends On |
|-----------|--------|------------|------------|
| Phase 2.3 remaining (telemetry, data lifecycle, adaptive thresholds) | [Project 2.3](https://linear.app/frenchforest/project/23-homeostasis-and-feedback-dbce3b171536) | `plans/PHASE_2.3_PLAN.md`, `specs/TRACEABILITY_AND_PERFORMANCE_SPEC.md` | Qwen3.5, tests passing |
| Phase 2.6 Conversational Agent MVP | [Project 2.6](https://linear.app/frenchforest/project/26-conversational-agent-mvp-40fbc8c41510) | `specs/CLI_SERVICE_CLIENT_SPEC.md`, `specs/CONVERSATION_CONTINUITY_SPEC.md` | Phase 2.3 |

## Backlog (needs approval)

| Work Item | Linear | Spec / ADR |
|-----------|--------|------------|
| Phase 2.4 Multi-Agent Orchestration | [Project 2.4](https://linear.app/frenchforest/project/24-multi-agent-orchestration-4c9ee23c6f51) | ADR-0017 |
| Phase 2.5 Seshat Memory Librarian | [Project 2.5](https://linear.app/frenchforest/project/25-seshat-memory-librarian-3d30e7d2d24f) | ADR-0018 |
| Phase 3.0 Daily-Use Interface | [Project 3.0](https://linear.app/frenchforest/project/30-daily-use-interface-60a517bd90f6) | — |
| Captain's Log ES Backfill | — | `specs/CAPTAINS_LOG_ES_BACKFILL_SPEC.md` |

## Completed

| Phase | Completed | Summary |
|-------|-----------|---------|
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
