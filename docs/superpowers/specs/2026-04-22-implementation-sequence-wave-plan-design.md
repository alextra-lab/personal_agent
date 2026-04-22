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
| 3b | FRE-245 | ADR draft | ADR-0054 drafted at `docs/architecture_decisions/ADR-0054-feedback-stream-bus-convention.md` — In Review, awaiting acceptance | 🔄 In Review 2026-04-22 |

**Gate:** FRE-245 must be accepted before any Phase 2 ADR is drafted.

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
