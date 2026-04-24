# Implementation Sequence — Wave Plan (Approved Items)

**Date**: 2026-04-22
**Status**: Approved
**Approach**: Hybrid B+C — critical-path ordering with interleaved code/spec work

---

## Principles

- **Critical path first (B):** FRE-244 and FRE-247 precede FRE-246 — they unblock Phase 3 faster. FRE-246 (Mode Manager) is critical but has no downstream dependencies in the ADR chain.
- **Interleave code and spec work (C):** Code fixes and investigations land between ADR drafting waves to avoid spec fatigue and to ensure findings (especially FRE-254) are available before stream ADRs are written.
- **Investigation before design:** FRE-254 (step-count investigation) runs in parallel with the foundational ADR (FRE-245) so its findings can influence stream ADR design choices.
- **Fix before formalize:** FRE-253 (DSPy cloud bug) is fixed before FRE-248 formalizes Stream 1 (self-improvement pipeline).

---

## Sequence

### Wave 0 — Immediate Code Wins
*No dependencies. Ship before any spec work begins.*

| # | Issue | Type | Notes |
|---|-------|------|-------|
| ~~1~~ | ~~FRE-253~~ | ~~Bug fix~~ | ~~DSPy bypassed for cloud models in `reflection.py` — self-reflection quality degraded right now~~ ✅ Done 2026-04-22 |
| ~~2~~ | ~~FRE-252~~ | ~~Feature~~ | ~~Per-TaskType tool allowlist in Stage 3 governance — independent, small scope~~ ✅ Done 2026-04-22 |

---

### Wave 1 — Foundation (Parallel)
*FRE-254 and FRE-245 proceed simultaneously.*

| # | Issue | Type | Notes |
|---|-------|------|-------|
| ~~3a~~ | ~~FRE-254~~ | ~~Investigation~~ | ~~Step-count reduction — findings at `docs/research/FRE-254-step-count-investigation.md`~~ ✅ Done 2026-04-22 |
| ~~3b~~ | ~~FRE-245~~ | ~~ADR + implementation~~ | ~~ADR-0054 accepted; flattened `EventBase` carries `trace_id`/`session_id`/`source_component`/`schema_version`; 10 producer sites migrated; 5 new tests; 118 tests pass~~ ✅ Done 2026-04-23 |

**Gate:** ~~FRE-245 must be accepted before any Phase 2 ADR is drafted.~~ ✅ Cleared 2026-04-23 — Wave 2 is unblocked.

---

### Wave 2 — Phase 2 ADRs + Implementations
*All depend on FRE-245 accepted. Ordered by critical-path impact (B), not criticality of breakage.*

| # | Issue | Type | Unblocks | Notes |
|---|-------|------|----------|-------|
| 4 | FRE-244 | ADR draft → impl | FRE-249 | ADR-0056: Error Pattern Monitoring — Level 3 observability; closes the gap in the four-level framework |
| 5 | FRE-247 | ADR draft → impl | FRE-250 | ADR-0057: Insights & Pattern Analysis — wires InsightsEngine to full loop; delegation patterns |
| 6 | FRE-246 | ADR draft → impl | — | ADR-0055: Mode Manager fix — critical disconnect (`app.py:176` hardcodes `Mode.NORMAL`); no Phase 3 deps |
| 7 | FRE-248 | ADR draft → impl | FRE-226 | ADR-0058: Self-Improvement Pipeline — formalizes Streams 1-3; adds `captain_log.entry_created` bus event |

**Note:** 4 and 5 can be drafted in parallel (different worktrees). 6 and 7 follow.
Each ADR draft → acceptance → implementation issues spun off → implementation → next ADR.

---

### Wave 3 — Phase 3 ADRs + Implementations
*Parallel. Each unblocked independently by Wave 2.*

| # | Issue | Type | Depends On | Notes |
|---|-------|------|------------|-------|
| 8 | FRE-249 | ADR draft → impl | FRE-244 impl | ADR-0059: Context Quality — compaction quality detection to full feedback loop |
| 9 | FRE-250 | ADR draft → impl | FRE-247 impl | ADR-0060: KG Quality — consolidation quality + decay scores to full loop |

FRE-249 and FRE-250 can proceed as soon as their respective Wave 2 implementations are done — they do not need to wait for each other.

---

### Wave 4 — Phase 4 ADRs + Implementations

| # | Issue | Type | Depends On | Notes |
|---|-------|------|------------|-------|
| 10 | FRE-251 | ADR draft → impl | FRE-249 impl | ADR-0061: Within-Session Progressive Context Compression (head-middle-tail) |
| 11 | FRE-226 | ADR draft → impl | FRE-248 impl | Agent self-updating skills (agentskills.io format) |

---

## Dependency Graph

```
Wave 0: FRE-253 (DSPy bug fix) ── FRE-252 (tool allowlist)
        ↓ prerequisite for FRE-248 formalization

Wave 1: FRE-254 (investigation) ─────────────────────────────── findings inform stream ADRs
        FRE-245 (ADR-0054) ──────────────────────────────────── foundation
                    │
        ┌───────────┼───────────┬─────────────────────┐
        ▼           ▼           ▼                     ▼
Wave 2: FRE-244   FRE-247    FRE-246               FRE-248
        ADR-0056  ADR-0057   ADR-0055              ADR-0058
        → impl    → impl     → impl                → impl
           │          │                                │
           ▼          ▼                                │
Wave 3: FRE-249   FRE-250                             │
        ADR-0059  ADR-0060                            │
        → impl    → impl                              │
           │                                           │
           ▼                                           ▼
Wave 4: FRE-251 (ADR-0061) → impl       FRE-226 → impl
```

---

## Key Risks

| Risk | Mitigation |
|------|------------|
| FRE-254 findings arrive after FRE-245 is accepted | FRE-254 runs in parallel with FRE-245 (not after); if findings are late, the bus convention ADR leaves stream-naming open for amendment |
| Phase 2 ADRs drafted before FRE-245 is accepted | Hard gate: no Phase 2 ADR drafted until ADR-0054 is in Accepted state |
| FRE-246 (Mode Manager) left broken during Wave 2 | Acceptable: Mode.NORMAL is the current behavior; it's not degrading, just not improving. Position 6 of 11 is within tolerance. |
| Wave 3 items start before Wave 2 implementations complete | Each Phase 3 ADR has an explicit predecessor in Linear (`blockedBy` FRE-244 impl, FRE-247 impl) |

---

## References

- `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md` — stream catalog and ADR sequence
- `docs/architecture_decisions/ADR-0053-gate-feedback-monitoring.md` — Feedback Stream ADR Template (all stream ADRs follow this)
- `docs/plans/MASTER_PLAN.md` — approved issue list and dependency graph

---

## Amendment 2026-04-24 — Wave 2.5 insertion (ADR-0063)

A parallel track opens on 2026-04-24: **ADR-0063 Primitive Tools & Action-Boundary Governance** (`docs/architecture_decisions/ADR-0063-primitive-tools-action-boundary-governance.md`). Migration plan: `docs/plans/2026-04-24-primitive-tools-migration-plan.md`.

### Relationship to the original wave plan

- Wave 2 (FRE-246 / 244 / 247 / 248) proceeds unchanged. No code intersections.
- **FRE-246** (Mode Manager, ADR-0055) is a hard prerequisite for PIVOT-2 (action-boundary governance reads mode state).
- **FRE-226** original scope splits:
  - Phase 1 (hand-authored skill docs) is **absorbed into PIVOT-3** — without skill docs the primitives in ADR-0063 cannot be driven reliably.
  - Phase 2 (agent writes its own skills via self-improvement) retains its Wave 4 position and continues to depend on ADR-0058 (FRE-248).
- **FRE-252** (Per-TaskType tool allowlist) is superseded in effect by ADR-0063 P1, which removes the consumer wire it established. FRE-252's Stage 4-before-Stage 3 pipeline reorder remains — that change is independent and correct.

### Insertion in wave sequence

| Wave | Tracks in flight |
|------|------------------|
| 2 | FRE-246 (Mode Manager) → FRE-244 / FRE-247 / FRE-248 (as originally planned) |
| 2.5 (parallel) | PIVOT-1 (sever filter wire) → PIVOT-2 (primitives + sandbox, blocked on FRE-246) → PIVOT-3 (skill docs + eval) → PIVOT-4 (flag-gated deprecation) → PIVOT-5 (loop gate split + model_config fix) → PIVOT-6 (delete legacy tools) |
| 3 | FRE-249 / FRE-250 — unchanged |
| 4 | FRE-251 and FRE-226 phase 2 — unchanged |

### Merge discipline

- Pivot PRs rebase on `main` before merge.
- 48h freeze on `request_gateway/governance.py` and `orchestrator/executor.py:step_llm_call` immediately after PIVOT-1 merge to capture baseline telemetry.
- No further freezes required.

### North-star note

ADR-0063 treats the six-phase migration as a **learning-oriented path** to a primitive-first, action-boundary-governed agent (Claude-Code-class composability). Each phase is independently reversible. The broader endpoint — collapsing all tool governance to action-boundary + skill docs — is held as the long-term direction but not forced in one pivot. Pivoting safely is worth more than pivoting fast.
