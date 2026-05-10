# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest)
> **Source of truth for priorities**: This file
> **Last updated**: 2026-05-10 — ADR-0068 chain ✅ (FRE-351/352/353/354/355/356); Wave J ✅; FRE-265 gate opens 2026-05-12; FRE-326 gate opens 2026-05-13

---

## Current State

Waves A ✅ B (partial) ✅ C ✅ E (FRE-213) ✅ J ✅ complete. ADR-0068 self-telemetry chain fully shipped (PRs #30–33): cloud emit parity, step rename, ES template reconciliation, self-telemetry skill doc, read primitive tail_lines. Next gates: FRE-265 legacy delete (2026-05-12), FRE-326 consolidation re-eval (2026-05-13). FRE-350 post-deploy eval earliest 2026-05-24 (2-week usage window).

Wave D implementation (endpoint abstraction, compose unification, test parity) remains deferred per FRE-214 audit §8.7 until further owner direction.

---

## Upcoming — Wave Sequence

| Wave | Theme | Status | Key Issues | Notes |
|------|-------|--------|------------|-------|
| **A** ✅ | Dev loop & hygiene | Done | FRE-309 · FRE-185/189/320/321/312/308 | All shipped 2026-05-08 |
| **B** ✅ (partial) | Self-observation | FRE-326 pending | FRE-301 ✅ · FRE-300 ✅ · FRE-319 ✅ · FRE-269 ✅ · [FRE-326](https://linear.app/frenchforest/issue/FRE-326) | FRE-326 gate ≥ 2026-05-13 |
| **C** ✅ | Security | Done | [FRE-225](https://linear.app/frenchforest/issue/FRE-225) ✅ | Shipped 2026-05-08 |
| **D** | Architecture | Planning ✅, impl deferred | [FRE-214](https://linear.app/frenchforest/issue/FRE-214) ✅ · FRE-238 · FRE-240 · FRE-241 · FRE-236 · FRE-336 · FRE-338–341 | Tracks 2a/2b/3 + follow-ups unblocked; deferred per audit §8.7 |
| **E** ✅ (partial) | Identity & write surface | FRE-342/343/344 pending | [FRE-213](https://linear.app/frenchforest/issue/FRE-213) ✅ · FRE-227 ⏸ · [FRE-342](https://linear.app/frenchforest/issue/FRE-342) · [FRE-343](https://linear.app/frenchforest/issue/FRE-343) · [FRE-344](https://linear.app/frenchforest/issue/FRE-344) · FRE-345 | FRE-342 HIGH unblocked now; FRE-227 paused → Backlog |
| **F** | Self-improvement | Partial | [FRE-328](https://linear.app/frenchforest/issue/FRE-328) · FRE-226 · FRE-234 | FRE-328 Phase 1 unblocked; FRE-226 needs FRE-227 |
| **G** | Cleanups & gates | Gates pending | [FRE-265](https://linear.app/frenchforest/issue/FRE-265) · [FRE-299](https://linear.app/frenchforest/issue/FRE-299) · [FRE-314](https://linear.app/frenchforest/issue/FRE-314) · [FRE-337](https://linear.app/frenchforest/issue/FRE-337) · FRE-311 | FRE-265 gate ≥ 2026-05-12; FRE-311 parked on FRE-302; FRE-337 new from Wave J |
| **H** | Memory / context value | Not started | [FRE-178](https://linear.app/frenchforest/issue/FRE-178) → [FRE-179](https://linear.app/frenchforest/issue/FRE-179) → [FRE-180](https://linear.app/frenchforest/issue/FRE-180) · [FRE-230](https://linear.app/frenchforest/issue/FRE-230) | FRE-178 → 179 → 180 chain |
| **I** | User feedback + meta-learning | Not started | [FRE-267](https://linear.app/frenchforest/issue/FRE-267) · [FRE-183](https://linear.app/frenchforest/issue/FRE-183) · [FRE-184](https://linear.app/frenchforest/issue/FRE-184) | — |
| **J** ✅ | Eval methodology hardening | Done | FRE-329–335 all shipped | See archive for Wave J details |

---

## Immediately Actionable (approved, no gate)

| Ticket | Priority | Tier | What |
|--------|----------|------|------|
| [FRE-342](https://linear.app/frenchforest/issue/FRE-342) | High | Sonnet | Person dedup must exclude `user_id`-bound nodes from match candidates |
| [FRE-328](https://linear.app/frenchforest/issue/FRE-328) | Medium | Sonnet | Emit `missing_skill_requested` event when read_skill called with unknown name |
| [FRE-337](https://linear.app/frenchforest/issue/FRE-337) | Medium | Opus | Skill nudge injection — per-skill behavioral directives to stop model ignoring loaded skills |
| [FRE-344](https://linear.app/frenchforest/issue/FRE-344) | Medium | Haiku | `display_name` config-driven seeding for non-owner CF Access users |
| [FRE-343](https://linear.app/frenchforest/issue/FRE-343) | Medium | Sonnet | Personal time-window retrieval — "what did we talk about N days ago" scoped to user_id |
| [FRE-341](https://linear.app/frenchforest/issue/FRE-341) | Low | Haiku | Prune unused `execution-service` gateway token from config |
| [FRE-314](https://linear.app/frenchforest/issue/FRE-314) | Medium | Sonnet | `feedback_history/` retention policy in DataLifecycleManager |
| [FRE-349](https://linear.app/frenchforest/issue/FRE-349) | Medium | Opus | Surface actionable Insights in agent context (G3 from FRE-346) |
| [FRE-299](https://linear.app/frenchforest/issue/FRE-299) | Low | Haiku | Flip `graph_quality_governance_enabled` gate (verify 14-day preconditions first) |

**Calendar-gated (approved but not yet startable):**
- **FRE-265** (Urgent) — delete 8 legacy tool modules; gate ≥ 2026-05-12
- **FRE-326** (Opus) — consolidation gate re-eval; gate ≥ 2026-05-13
- **FRE-350** (Opus) — post-deploy reflection-surfacing eval; gate ≥ 2026-05-24

---

## Needs Approval

| Work Item | Notes |
|-----------|-------|
| **FRE-315: Mermaid chart rendering** | [FRE-315](https://linear.app/frenchforest/issue/FRE-315) — FRE-316/317/318 closed as duplicates |

---

## Key Dependencies

```
FRE-213 ✅ → FRE-342/343/344 (unblocked) → FRE-227 (paused) → FRE-226 (self-updating skills)
FRE-178 → FRE-179 → FRE-180  (recall L2/L3/gap chain)
FRE-214 ✅ → FRE-238/240/241/236 + FRE-336 + FRE-338–341 (unblocked, deferred §8.7)
FRE-263 ✅ → FRE-265 (delete) gate ≥ 2026-05-12
FRE-325 ✅ → FRE-326 (consolidation re-eval) gate ≥ 2026-05-13
FRE-348 ✅ → FRE-350 (eval) gate ≥ 2026-05-24
FRE-346 ✅ → FRE-347 ✅ → FRE-348 ✅ → FRE-349 (G3, unblocked)
ADR-0068 ✅ (FRE-351/352/353/354/355/356 all done)
FRE-302 ✅ → FRE-311 (budget auto-tuning, parked pending data)
```

---

## Recently Completed

| Item | Date | Summary |
|------|------|---------|
| **FRE-355: read primitive tail_lines (ADR-0068 D6)** | 2026-05-10 | `tail_lines: int \| None` on read executor. Seeks backward in 4 KiB blocks from EOF; bypasses `max_bytes` size gate; caps output at `max_bytes`. Resolves `current.jsonl` (19 MB) inaccessibility. 5 new tests. PR #33. |
| **FRE-356: self-telemetry.md skill doc (ADR-0068 D7)** | 2026-05-10 | New `docs/skills/self-telemetry.md` — 43-keyword frontmatter, 5 canonical bash+curl patterns (token stats, cache hit rate, cost by model_role, interaction outcomes, per-trace latency). PR #32. |
| **FRE-353: ES index template reconcile (ADR-0068 D4)** | 2026-05-10 | Deleted 4 dead field declarations; added 8 explicit typed mappings; fixed 3 type mismatches (`total_tokens` int→long, `latency_ms` float→long, `cost_usd` float→double). PR #31. |
| **FRE-351/352/354: Cloud emit parity + step rename + skill-doc fix** | 2026-05-10 | `litellm_request_complete` field parity; executor step renamed to `llm_step_completed`; `query-elasticsearch.md` run_python import bug fixed. PR #30. |
| **FRE-348: Reflection surfacing in context assembly (ADR-0067 G2)** | 2026-05-10 | `captains_log/recall.py` + context injection of up to 3 relevant reflections. 12 tests. PR #29. |
| **FRE-347: session_summary generation (G1)** | 2026-05-09 | `second_brain/session_summary.py` — LLM-generated session summary on every consolidation pass. 10 tests. |
| **FRE-213: Seshat owner identity (ADR-0052 amended)** | 2026-05-09 | Owner identity primitive; `user_id` anchor; operator stanza; `get_or_provision_user_person()`. 26 tests. PR #28. |
| **FRE-214: VPS topology audit + Wave D plans** | 2026-05-09 | Ratified full-harness-on-VPS; 30-row parity matrix; 3 Sonnet-ready impl plans; ADR-0045 amended. |

*Older items archived → `docs/plans/completed/2026-05-10-completed-archive.md`*

---

## Active ADRs

| ADR | Title | Status |
|-----|-------|--------|
| **0068** | **Agent Self-Telemetry Data Plane** | **Accepted 2026-05-10; all 7 follow-ups shipped (FRE-351–356)** |
| **0067** | **Reflection Surfacing in Context Assembly** | **Accepted 2026-05-10 (FRE-348); eval → FRE-350** |
| **0066** | **Skill Routing Defaults + Threshold + Feedback Loop** | Accepted 2026-05-07; D1–D5 all implemented; eval complete (Wave J) |
| 0065 | Cost Check Gate — Atomic Reservation | Accepted + Implemented; FRE-311 parked |
| 0063 | Primitive Tools & Action-Boundary Governance | In progress — FRE-265 delete pending gate ≥ 2026-05-12 |
| 0061 | Within-Session Progressive Context Compression | Accepted + Implemented (FRE-251) |
| 0060 | Knowledge Graph Quality Stream | Accepted + Implemented; Phase 2 flag-gated → FRE-299 flip |
| 0052 | Seshat Owner Identity Primitive | Accepted (amended 2026-05-09) + Implemented — Wave E follow-ups FRE-342/343/344/345 |
| 0041 | Event Bus — Redis Streams | Accepted; Phases 1–4 live |
| 0040 | Linear as Async Feedback Channel | Accepted; Phases 1–2 live; Phase 3 → FRE-183 |

*Full ADR list: `docs/architecture_decisions/`*

---

## How This File Works

- **Linear is the task tracker** — this file tracks priorities and sequencing only.
- **Next task**: pick from "Immediately Actionable" table above, highest priority first.
- **Update after every ship**: add to Recently Completed, bump Last updated.
- **Specs** → `docs/specs/` · **ADRs** → `docs/architecture_decisions/` · **Session plans** → `docs/superpowers/plans/`
- **Archive** → `docs/plans/completed/` (items older than ~1 week)
