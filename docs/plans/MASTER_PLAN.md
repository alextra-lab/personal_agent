# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest)
> **Source of truth for priorities**: This file
> **Last updated**: 2026-05-13 — FRE-341 token pruned. FRE-344 display-name seeding PR #50 merged. FRE-299 governance gate flipped (33 anomalies, governance_enabled=True confirmed in logs).

---

## Current State

Waves A ✅ B (partial) ✅ C ✅ E (FRE-213) ✅ J ✅ complete. ADR-0063 (primitive tools) fully closed with FRE-265 legacy-tools deletion. ADR-0068 self-telemetry chain fully shipped (PRs #30–33). Next gates: FRE-326 consolidation re-eval (2026-05-13). FRE-350 post-deploy eval earliest 2026-05-24 (2-week usage window).

Wave D implementation (endpoint abstraction, compose unification, test parity) remains deferred per FRE-214 audit §8.7 until further owner direction.

---

## Upcoming — Wave Sequence

| Wave | Theme | Status | Key Issues | Notes |
|------|-------|--------|------------|-------|
| **A** ✅ | Dev loop & hygiene | Done | FRE-309 · FRE-185/189/320/321/312/308 | All shipped 2026-05-08 |
| **B** ✅ (partial) | Self-observation | FRE-326 pending | FRE-301 ✅ · FRE-300 ✅ · FRE-319 ✅ · FRE-269 ✅ · [FRE-326](https://linear.app/frenchforest/issue/FRE-326) | FRE-326 gate ≥ 2026-05-13 |
| **C** ✅ | Security | Done | [FRE-225](https://linear.app/frenchforest/issue/FRE-225) ✅ | Shipped 2026-05-08 |
| **D** | Architecture | Planning ✅, impl deferred | [FRE-214](https://linear.app/frenchforest/issue/FRE-214) ✅ · FRE-238 · FRE-240 · FRE-241 · FRE-236 · FRE-336 · FRE-338–340 · [FRE-341](https://linear.app/frenchforest/issue/FRE-341) ✅ | Tracks 2a/2b/3 + follow-ups unblocked; deferred per audit §8.7; FRE-341 shipped 2026-05-13 |
| **E** ✅ (partial) | Identity & write surface | FRE-343 pending | [FRE-213](https://linear.app/frenchforest/issue/FRE-213) ✅ · FRE-227 ⏸ · [FRE-342](https://linear.app/frenchforest/issue/FRE-342) ✅ · [FRE-343](https://linear.app/frenchforest/issue/FRE-343) · [FRE-344](https://linear.app/frenchforest/issue/FRE-344) 🔵 · FRE-345 | FRE-227 paused → Backlog; FRE-344 PR #50 In Review |
| **F** | Self-improvement | Partial | [FRE-328](https://linear.app/frenchforest/issue/FRE-328) 🅿️ · FRE-226 · FRE-234 | FRE-328 pipeline shipped (PRs #43–47), parked 2026-05-12 for natural-usage eval; review gate 2026-05-26. FRE-226 needs FRE-227 |
| **G** | Cleanups & gates | Partial | [FRE-265](https://linear.app/frenchforest/issue/FRE-265) ✅ · [FRE-299](https://linear.app/frenchforest/issue/FRE-299) ✅ · [FRE-314](https://linear.app/frenchforest/issue/FRE-314) · [FRE-337](https://linear.app/frenchforest/issue/FRE-337) ✅ · FRE-311 | FRE-265 shipped 2026-05-12; FRE-337 shipped 2026-05-13 (PR #48); FRE-299 gate flipped 2026-05-13; FRE-311 parked on FRE-302 |
| **H** | Memory / context value | Not started | [FRE-178](https://linear.app/frenchforest/issue/FRE-178) → [FRE-179](https://linear.app/frenchforest/issue/FRE-179) → [FRE-180](https://linear.app/frenchforest/issue/FRE-180) · [FRE-230](https://linear.app/frenchforest/issue/FRE-230) | FRE-178 → 179 → 180 chain |
| **I** | User feedback + meta-learning | Not started | [FRE-267](https://linear.app/frenchforest/issue/FRE-267) · [FRE-183](https://linear.app/frenchforest/issue/FRE-183) · [FRE-184](https://linear.app/frenchforest/issue/FRE-184) | — |
| **J** ✅ | Eval methodology hardening | Done | FRE-329–335 all shipped | See archive for Wave J details |

---

## Immediately Actionable (approved, no gate)

| Ticket | Priority | Tier | What |
|--------|----------|------|------|
| [FRE-343](https://linear.app/frenchforest/issue/FRE-343) | Medium | Sonnet | Personal time-window retrieval — "what did we talk about N days ago" scoped to user_id |
| [FRE-314](https://linear.app/frenchforest/issue/FRE-314) | Medium | Sonnet | `feedback_history/` retention policy in DataLifecycleManager |
| [FRE-349](https://linear.app/frenchforest/issue/FRE-349) | Medium | Opus | Surface actionable Insights in agent context (G3 from FRE-346) |

**Calendar-gated (approved but not yet startable):**
- **FRE-326** (Opus) — consolidation gate re-eval; gate ≥ 2026-05-13
- **FRE-350** (Opus) — post-deploy reflection-surfacing eval; gate ≥ 2026-05-24
- **FRE-328** (Opus) — naming-stability decision after two weeks of passive capture; gate ≥ 2026-05-26 — see `docs/plans/fre-328-evaluation-window.md`

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
FRE-263 ✅ → FRE-265 ✅ (delete, 2026-05-12)
FRE-325 ✅ → FRE-326 (consolidation re-eval) gate ≥ 2026-05-13
FRE-348 ✅ → FRE-350 (eval) gate ≥ 2026-05-24
FRE-328 capture pipeline ✅ → FRE-328 naming-stability decision gate ≥ 2026-05-26
FRE-346 ✅ → FRE-347 ✅ → FRE-348 ✅ → FRE-349 (G3, unblocked)
ADR-0068 ✅ (FRE-351/352/353/354/355/356 all done)
FRE-302 ✅ → FRE-311 (budget auto-tuning, parked pending data)
```

---

## Recently Completed

| Item | Date | Summary |
|------|------|---------|
| **FRE-299: Flip graph_quality_governance_enabled gate (ADR-0060 P2-3)** | 2026-05-13 | Set `AGENT_GRAPH_QUALITY_GOVERNANCE_ENABLED=true` in `.env` after 14-day Phase 1 validation (33 anomalies published, 0% false-positive rate). `governance_enabled=True` confirmed in gateway startup logs. |
| **FRE-341: Prune execution-service gateway token** | 2026-05-13 | Deleted stale `execution-service` token block from `config/gateway_access.yaml`. ADR-0045 amended 2026-05-08 confirmed orchestrator and gateway are the same process — the role was never used. Config-only change, no tests needed. Direct-to-main commit. |
| **FRE-337: Skill nudge injection + XML headers (ADR-0067)** | 2026-05-13 | Two deterministic XML directive blocks (`<skill_index_directive>` and `<skill_usage_directives>`) appended after all skill content in the system prompt, immediately before the user message. Closes the behavioral gap where the primary model (recall=1.0 from router) answered from training-data priors instead of executing the injected skill. `nudge:` YAML frontmatter field added to `SkillDoc`; seeded on 4 skills. Feature-gated via `AGENT_SKILL_NUDGE_ENABLED`. ADR-0067 accepted. PR #48. Follow-up PR #49: `SKILL_BLOCK_HEADER` wrapped in `<skill_library>` XML; redundant `read_skill` instruction removed from index header. Eval: Family A 5/5 live tool use (was 0/5), Family B 4/4 knowledge-only (no regression), adversarial guard held. |
| **FRE-328: capability-gap capture → Linear pipeline (parked for eval)** | 2026-05-12 | Full pipeline shipped across PRs #43–47 and verified end-to-end on the live gateway.  Two signal sources feed the same ES bucket and aggregation: (a) `read_skill` emits `missing_skill_requested` when called with an unknown name; (b) DSPy reflection populates `missing_skill_names` after each task, parsed and emitted from the main loop with `source="reflection"`.  `TelemetryQueries.get_missing_skill_buckets` aggregates by `requested_name` with a cardinality sub-agg on `session_id`.  `InsightsEngine.detect_missing_skill_patterns` produces an `Insight` when count ≥3 across ≥2 distinct sessions, which then flows through `create_captain_log_proposals` → `CaptainLogManager.save_entry()` → `PromotionPipeline` → `Needs Approval` Linear ticket.  Field test surfaced a naming-stability problem: the reflection model names the same conceptual gap differently across sessions (e.g. `word-count-log`, `word-count-history`, `word-count-log-update`, `rolling-sum-7-day`), so every bucket sits at count=1 and the threshold cannot fire without name normalization.  **Parked 2026-05-12 to capture data naturally**; review gate 2026-05-26 with candidate normalization approaches documented in `docs/plans/fre-328-evaluation-window.md`.  Three reusable lessons captured there: (1) `asyncio.to_thread` logs don't reach the ES handler; (2) structlog `event` is stored as ES field `event_type`, no `.keyword` sub-field on already-keyword fields; (3) LLM-generated identifiers are not stable across sessions — any pipeline that keys on them for cardinality must normalize. |
| **FRE-265: Delete 8 legacy tool modules (ADR-0063 P6)** | 2026-05-12 | After 14 days of `AGENT_LEGACY_TOOLS_ENABLED=false` untouched in production, removed: 7 tool modules (`filesystem`, `system_health`, `self_telemetry`, `elasticsearch`, `fetch`, `sysdiag`, `infra_health`) covering 8 tool surfaces, 6 obsolete test files, governance entries in `tools.yaml`, the `legacy_tools_enabled` setting + `AGENT_LEGACY_TOOLS_ENABLED` env var (incl. `docker-compose.eval.yml` control profile and `.env.example`), the deprecated `GovernanceContext.allowed_tool_categories` field (FRE-260 stub), and the unused `allowed_categories` param from `ToolRegistry.get_tool_definitions_for_llm`. Stripped dead tool name references from intent-pattern regex, fallback reply text, and deployment-hint scaffolding in `orchestrator/executor.py`. Updated `prompts.py` to point at `bash` + skill docs for fetch/list operations. Tests rewritten (`test_primitives_registration.py`, `test_legacy_tools_deprecation.py` removed). Net −20 ruff and −10 mypy errors. ADR-0063 fully closed; `git revert` is now the rollback path. |
| **FRE-342: Person dedup excludes user_id-bound :Person** | 2026-05-12 | `memory/dedup._find_similar_entities` Cypher gains `AND node.user_id IS NULL` so harness owner/user-anchored `:Person` nodes (FRE-213 schema, ADR-0052 amendment) are never returned as merge candidates. Prevents extracted third-party "Alex" from colliding into `:Person {user_id, is_owner:true}` and destroying the anchor. Unit test asserts filter present in issued Cypher. PR #41. |
| **Post-PR-#34 eval stabilization (PRs #35–40)** | 2026-05-11 | Six PRs in one day. (1) Harness alignment for PR #34 changes: ES `trace_id.keyword`→pure keyword, `--cf-email` flag for cloud profile auth, retry config for 5s ES refresh — PR #35. (2) Config aligned with Qwen3.6-35B-A3B card: `context_length` 64K→**131072**, sampling to "Thinking — General Tasks", `thinking_budget_tokens` 3K→32K, budgets up to **120K max/96K window**, harness timeouts bumped — PR #36. (3) `thinking_budget_tokens` calibration to 16K — PR #37 (later reverted). (4) Revert PR #37 after diagnosis showed sub-agent failures, not budget overruns, were the root cause — PR #38. (5) Swap sub-agent from `mlx-community/Qwen3.5-9B-8bit` to a second instance of Qwen3.6-A3B (`unsloth/qwen3.6-35-A3B-subagent`, Instruct preset, 16K, no thinking) — PR #39. Sub-agent layer measured: success rate 24% → **100%**, avg latency 39s → **22s**. (6) Primary `temperature` 1.0 → **0.6** after EVAL-2026-05-11-subagent-qwen36 showed three of four remaining failures were temp-variance driven — PR #40. Targeted re-run confirmed CP-05 and CP-24 fixed (CP-24 was previously suspected architectural). Remaining 2 known failures (CP-01 turn 2, CP-20 turn 1) are test-fragility — Qwen3.6 makes reasonable choices the tests forbid. Full eval progression: 33/37 (broken) → 34/37 (clean) → projected 36/37 with temp 0.6 now on main. Plan + diagnoses in `docs/superpowers/plans/analyze-the-results-and-immutable-lerdorf.md` and four `telemetry/evaluation/EVAL-2026-05-1X-*/COMPARISON.md` files. |
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
| 0063 | Primitive Tools & Action-Boundary Governance | Accepted + Implemented (P1–P6 complete via FRE-260/261/262/263/264/265) |
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
