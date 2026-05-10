# Completed Work Archive — up to 2026-05-10

Items moved here from MASTER_PLAN.md during cleanup on 2026-05-10.
Current items (2026-05-08 onwards) remain in MASTER_PLAN.md.

---

## Wave J — Skill Routing Eval Methodology (all 7 shipped 2026-05-08)

Execution order: FRE-329 → FRE-331 → FRE-330 → FRE-334 → FRE-332/333 → FRE-335

Key findings: keyword/hybrid `es_first_call_correct_rate` drops 100%→45% with realistic prompts; `model_decided` maintains 100% ES routing + recall=0.95. ADR-0066 D2 threshold monitor live (FRE-335).

| Item | Summary |
|------|---------|
| **FRE-329** | Fixed `or`/`and` analysis bug in `es_first_call_correct_rate`; re-analysed Phase D data |
| **FRE-331** | Router-only vs end-to-end metric split + ground-truth labels |
| **FRE-330** | Re-ran cloud/local model_decided cells post-router-fix |
| **FRE-334** | Expanded prompt set: 26 prompts (ambiguous + negative-control + adversarial) |
| **FRE-332 + FRE-333** | ES polling instead of fixed 5s sleep; pagination past size=500 with search_after |
| **FRE-335** | Captain's Log p95 monitor (ADR-0066 D2) — auto-files Linear ticket at 6,000-token threshold |

---

## Skill Routing — Phases A–D (shipped 2026-05-06)

| Item | PR | Summary |
|------|-----|---------|
| Phase A — frontmatter auto-discovery | #20 | Replaced hardcoded keyword routes with glob + YAML frontmatter; 14 self-describing skill docs |
| Phase B — skill index + read_skill + hybrid | #22 | read_skill tool; 280-tok compact index; hybrid routing mode; dedup via ctx.loaded_skills |
| Phase C — separate routing model | #23 | skill_routing_model_key (default: claude_haiku); independent of primary agent path |
| Phase D — eval harness | — | 6-cell matrix runnable end-to-end; per-request skill_routing_mode override |
| ADR-0066 | — | Locks hybrid default; p95 threshold 6,000 tokens; D4 budget role fix |
| FRE-327 | #19 | neo4j-direct.md skill doc |
| FRE-325 | #18 | Remove brainstem polling loop — consolidation now event-driven |

---

## Wave B — Self-observation (shipped 2026-05-08)

FRE-300: warning allowlist wired into error monitor. FRE-301: hit_iteration_limit in Captain's Log reflection. FRE-319: cloud model config drift fixed. FRE-269: 19 wrong-prefix vars fixed in .env.example.

## Wave A — Dev loop & hygiene (shipped 2026-05-08)

FRE-309: Linear label lookup workspace fallback. FRE-185/189/320/321/312/308: all no-ops — already resolved by prior work.

## Wave C — Security (shipped 2026-05-08)

FRE-225: DomainGuard in security.py — URLhaus feed, disk cache, blocklist/allowlist. 19 tests.

---

## Earlier items (pre-2026-05-08)

| Item | Date | Summary |
|------|------|---------|
| FRE-323 + FRE-324: Memory recovery | 2026-05-05 | Cypher ON CREATE SET fix; synthesis nudge after tool results. PRs #16/#17. |
| FRE-251: Within-session compression (ADR-0061) | 2026-05-01 | Head-middle-tail invariant; tool-output pre-pass; token budget threshold trigger. PR #10. |
| FRE-250: Knowledge Graph Quality (ADR-0060) | 2026-04-30 | Tier reranking, decay, Streams 6+8 closed, Phase 2 flag-gated (→ FRE-299 flip gate). |
| FRE-263: Deprecate 8 legacy tools (ADR-0063 P4) | 2026-04-28 | AGENT_LEGACY_TOOLS_ENABLED=false. Gate: 2-week window → FRE-265 (delete). |
| FRE-261: Primitive tools + sandbox (ADR-0063 P2) | 2026-04-27 | bash, read, write, run_python sandbox, AG-UI approval round-trip. |
| FRE-249: Context Quality (ADR-0059) | 2026-04-27 | Compaction quality detection, full feedback loop. |
| FRE-235 + FRE-268 + FRE-229: PWA + identity + visibility | 2026-04-26 | Session persistence, CF Access scoping, Neo4j visibility. |

*Pre-April 2026 foundation — Redesign v2 Slices 1–3, Seshat v2, Event Bus, KG Freshness, Context Intelligence, Proactive Memory — see other files in `docs/plans/completed/` and ADRs 0028–0060.*
