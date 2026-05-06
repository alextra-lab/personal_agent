# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest)
> **Source of truth for priorities**: This file
> **Last updated**: 2026-05-06 (skill routing Phase D eval harness ready; cloud cells runnable)

---

## Current State

System is healthy post-recovery. Memory round-trip passing. All Wave 2 recovery items closed (FRE-323 · FRE-324 · FRE-325 · FRE-327). Legacy tools deprecated behind flag (`AGENT_LEGACY_TOOLS_ENABLED=false`) — deletion window opens 2026-05-12 (FRE-265).

**Next task: Wave A → [FRE-309](https://linear.app/frenchforest/issue/FRE-309)** — fix Linear label lookup so the agent can file issues reliably.

---

## Upcoming — 9-Wave Sequence

*Full triage rationale: `plans/complete-next-task-in-iterative-leaf.md`*

| Wave | Theme | Work Items | Key Issues | Notes |
|------|-------|-----------|------------|-------|
| **A** ⬅ *next* | Dev loop & hygiene | Fix Linear label lookup; mcp import error; flaky Neo4j test; skill-injection tests; primitive_tools default drift; stale 74-failure sweep; consolidate plan storage | [FRE-309](https://linear.app/frenchforest/issue/FRE-309) · [FRE-185](https://linear.app/frenchforest/issue/FRE-185) · [FRE-189](https://linear.app/frenchforest/issue/FRE-189) · [FRE-320](https://linear.app/frenchforest/issue/FRE-320) · [FRE-321](https://linear.app/frenchforest/issue/FRE-321) · [FRE-312](https://linear.app/frenchforest/issue/FRE-312) · [FRE-308](https://linear.app/frenchforest/issue/FRE-308) | **FRE-309 first** — broken label lookup poisons agent self-filing |
| **B** | Self-observation | `hit_iteration_limit` in reflection; error monitor scans warnings; model_config audit; env.example audit; consolidation gate re-eval | [FRE-301](https://linear.app/frenchforest/issue/FRE-301) · [FRE-300](https://linear.app/frenchforest/issue/FRE-300) · [FRE-319](https://linear.app/frenchforest/issue/FRE-319) · [FRE-269](https://linear.app/frenchforest/issue/FRE-269) · [FRE-326](https://linear.app/frenchforest/issue/FRE-326) | FRE-326 scheduled ≥ 2026-05-13 |
| **C** | Security | Domain guard — block known malicious sites | [FRE-225](https://linear.app/frenchforest/issue/FRE-225) | — |
| **D** | Architecture | VPS+CF+local topology review (gates D2-D6); containerization decision; SLM circuit breaker; reranker fallback; slm_server supervisor; PWA iOS SSE | [FRE-214](https://linear.app/frenchforest/issue/FRE-214) · [FRE-217](https://linear.app/frenchforest/issue/FRE-217) · [FRE-238](https://linear.app/frenchforest/issue/FRE-238) · [FRE-240](https://linear.app/frenchforest/issue/FRE-240) · [FRE-241](https://linear.app/frenchforest/issue/FRE-241) · [FRE-236](https://linear.app/frenchforest/issue/FRE-236) | FRE-214 verdict gates D2-D6 |
| **E** | Identity & write surface | Seshat owner identity (ADR-0052); protected agent write dir | [FRE-213](https://linear.app/frenchforest/issue/FRE-213) · [FRE-227](https://linear.app/frenchforest/issue/FRE-227) | FRE-227 prereq for FRE-226 |
| **F** | Self-improvement | Self-updating skills phase 2 (ADR + impl); adaptive self-query arch; trigger effectiveness analysis | [FRE-226](https://linear.app/frenchforest/issue/FRE-226) · [FRE-258](https://linear.app/frenchforest/issue/FRE-258) · [FRE-234](https://linear.app/frenchforest/issue/FRE-234) | FRE-226 needs FRE-227; FRE-258 Tier-1 Opus |
| **G** | Cleanups & gates | Delete legacy tool code; flip graph_quality gate; feedback_history retention; budget auto-tuning (parked) | [FRE-265](https://linear.app/frenchforest/issue/FRE-265) · [FRE-299](https://linear.app/frenchforest/issue/FRE-299) · [FRE-314](https://linear.app/frenchforest/issue/FRE-314) · [FRE-311](https://linear.app/frenchforest/issue/FRE-311) | FRE-265 gate ≥ 2026-05-12; FRE-311 parked on FRE-302 |
| **H** | Memory / context value | Recall L2; Recall L3 LLM-judge; Context Gap Score; geolocation memory | [FRE-178](https://linear.app/frenchforest/issue/FRE-178) · [FRE-179](https://linear.app/frenchforest/issue/FRE-179) · [FRE-180](https://linear.app/frenchforest/issue/FRE-180) · [FRE-230](https://linear.app/frenchforest/issue/FRE-230) | FRE-178 → 179 → 180 chain |
| **I** | User feedback + meta-learning | PWA thumbs feedback; Feedback Channel Phase 3; Phase 4 eval | [FRE-267](https://linear.app/frenchforest/issue/FRE-267) · [FRE-183](https://linear.app/frenchforest/issue/FRE-183) · [FRE-184](https://linear.app/frenchforest/issue/FRE-184) | — |

---

## Needs Approval

| Work Item | Notes |
|-----------|-------|
| Mermaid chart rendering in chat UI | [FRE-315](https://linear.app/frenchforest/issue/FRE-315) canonical — FRE-316/317/318 closed as duplicates 2026-05-06 |

---

## Key Dependencies

```
FRE-213 (owner identity) → FRE-227 (write dir) → FRE-226 (self-updating skills)
FRE-178 (Recall L2) → FRE-179 (L3 judge) → FRE-180 (context gap score)
FRE-214 (arch review) → FRE-217 / FRE-238 / FRE-240 / FRE-241 / FRE-236
FRE-265 (legacy delete) — calendar gate ≥ 2026-05-12
FRE-326 (consolidation gates) — telemetry gate ≥ 2026-05-13
FRE-311 (budget auto-tuning) — parked until FRE-302 (ADR-0065) lands
```

---

## Recently Completed

| Item | Date | Summary |
|------|------|---------|
| Skill routing Phase D: eval harness + per-request override | 2026-05-06 | 6-cell matrix (3 cloud runnable now), 10 prompts, analysis script; per-request skill_routing_mode override added to /chat + harness — no restarts between cells. Run: `ENV=cloud make eval-skill-routing-cloud RUN=<id>` |
| Skill routing Phase C: separate routing model | 2026-05-06 | `skill_routing_model_key` (default: claude_haiku); `get_llm_client_for_key()`; `route_skills()` pre-flight call; independent of primary agent path; `ctx.skill_routing_done` prevents re-fire. PR #23. |
| Skill routing Phase B: skill index + read_skill + hybrid | 2026-05-06 | `read_skill` tool (model pulls full doc on demand); compact 280-tok index; hybrid routing mode; dedup via `ctx.loaded_skills`; sub-agent inheritance; post-exec hint. PR #22 (also includes B.5 guards). |
| Skill routing Phase A: frontmatter auto-discovery | 2026-05-06 | Replaced hardcoded `_SKILL_FILES`/`_KEYWORD_ROUTES` with glob + YAML frontmatter. 14 skill docs self-describing. Natural-language ES keywords ("show me logs", "check your logs"). Contract tests. PR #20. |
| FRE-327: Neo4j direct Cypher skill doc | 2026-05-06 | `docs/skills/neo4j-direct.md` + keyword route in `skills.py`. Agent self-diagnoses Neo4j in ≤6 calls. PR #19. |
| FRE-325: Remove brainstem polling loop | 2026-05-06 | Deleted `_monitoring_loop` / `system.idle` path; consolidation now purely event-driven. PR #18. |
| FRE-323 + FRE-324: Memory recovery | 2026-05-05 | Cypher `ON CREATE SET` fix (PR #16); synthesis nudge after tool results (PR #17). Memory round-trip passing. |
| FRE-251: Within-session compression (ADR-0061) | 2026-05-01 | Head-middle-tail invariant; tool-output pre-pass; triggered by token budget threshold. PR #10. |
| FRE-250: Knowledge Graph Quality (ADR-0060) | 2026-04-30 | Tier reranking, decay, Streams 6+8 closed, Phase 2 governance flag-gated. |
| FRE-261: Primitive tools + sandbox (ADR-0063 P2) | 2026-04-27 | `bash`, `read`, `write`, `run_python` sandbox, AG-UI approval round-trip, PWA ApprovalModal. |
| FRE-249: Context Quality (ADR-0059) | 2026-04-27 | Compaction quality detection, full feedback loop. |
| FRE-235 + FRE-268 + FRE-229: PWA + identity + visibility | 2026-04-26 | Session persistence, CF Access scoping, Neo4j visibility (public/group/private). |
| FRE-263: Deprecate 8 legacy tools (ADR-0063 P4) | 2026-04-28 | `AGENT_LEGACY_TOOLS_ENABLED=false`. 2-week window ends 2026-05-12 → FRE-265. |

*Pre-April 2026 foundation (Redesign v2 Slices 1-3, Seshat v2, Event Bus, KG Freshness, Context Intelligence, Proactive Memory) — see `docs/plans/completed/` and ADRs 0028–0060.*

---

## Active ADRs

| ADR | Title | Status |
|-----|-------|--------|
| 0063 | Primitive Tools & Action-Boundary Governance | In progress — FRE-265 (delete) pending gate |
| 0061 | Within-Session Progressive Context Compression | Accepted + Implemented (FRE-251) |
| 0060 | Knowledge Graph Quality Stream | Accepted + Implemented; Phase 2 flag-gated |
| 0052 | Seshat Owner Identity Primitive | Proposed — FRE-213 next in Wave E |
| 0041 | Event Bus — Redis Streams | Accepted; Phases 1–4 live |
| 0040 | Linear as Async Feedback Channel | Accepted; Phases 1–2 live; Phase 3 → Wave I |
| 0039 | Proactive Memory | Accepted (MVP); EVAL A/B numbers pending |

*Full ADR list: `docs/architecture_decisions/`*

---

## How This File Works

- **Linear is the task tracker** — this file tracks priorities and sequencing only.
- **Next task is always Wave A row 1** until Wave A closes, then Wave B row 1, etc.
- **Update after every ship**: mark item done, move to Recently Completed, bump Last updated.
- **Specs** → `docs/specs/` · **ADRs** → `docs/architecture_decisions/` · **Session plans** → `docs/superpowers/plans/`
