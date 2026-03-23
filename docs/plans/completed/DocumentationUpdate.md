# Documentation Update: Post Slices 1 & 2

**Date**: 2026-03-21
**Context**: Slices 1 & 2 of the Cognitive Architecture Redesign v2 were implemented 2026-03-20 (FRE-125 through FRE-144). The project is now in an **evaluation phase** — using the agent to build real usage history and traces to evaluate what was implemented, after which Slice 3 begins. Documentation is stale — it still describes the old Phase 2.1 design (role-switching, specialist agents, router SLM) with no mention of the gateway, delegation, sub-agents, or memory promotion. Several architecture docs are completely superseded. The Vision Doc has broken file references. This creates confusion for future development and collaboration.

**Project status**: Slices 1 & 2 Implemented → Evaluation & Data Collection → Slice 3 Planning

**Tier**: Tier-3 (Haiku) — mechanical text edits from detailed plan, no design decisions.

---

## Group A: High-Impact Updates (3 files)

### 1. `README.md` (root)

**A. Replace Architecture diagram** (lines 15-55)
- Change heading from `**Service-Based Design (Phase 2.1+)**` to `**Cognitive Architecture (Redesign v2)**`
- New diagram shows redesign layers: Interface → Pre-LLM Gateway (7 stages) → Primary Agent → Tools/Seshat/Expansion → Brainstem → Self-Improvement → Infrastructure
- Source: Section 2.1 of `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md`, simplified for README

**B. Replace Features section** (lines 57-85)
- **"Cognitive Architecture Redesign v2 (Current)"**: Pre-LLM Gateway, single-brain architecture, request gateway intent classification, decomposition & expansion (SINGLE/HYBRID/DECOMPOSE/DELEGATE), Stage B delegation, Seshat MemoryProtocol with promote(), expansion budget, context budget, insights engine
- **"Foundation (Complete)"**: Collapse old Phase 2.1 + 2.2 + 2.3 into one section

**C. Update Project Structure** (lines 279-313)
- Add `request_gateway/` (10 files), `insights/` (2 files), `memory/` (7 files)
- Update `orchestrator/` comment to mention sub-agents + HYBRID expansion

**D. Update Documentation links** (lines 315-331)
- Add link to Redesign v2 spec under Architecture
- Add links to Slice 1 & 2 plans under Development

### 2. `docs/plans/MASTER_PLAN.md`

**A. Update `Last updated`** to 2026-03-21

**B. Replace Current Focus table** — Remove stale items, add:
- Post-Slice 1&2 docs update (In Progress)
- Evaluation & data collection: using agent to build real usage traces (In Progress)
- Qwen3.5 integration (In Progress)

**C. Replace Upcoming** — Remove old Phase 2.3/2.6 entries, add:
- Slice 3: Intelligence — blocked on evaluation data from current usage phase
- Phase 2.3 remaining (data lifecycle, adaptive thresholds)

**D. Replace Backlog** — Remove Phase 2.4 (superseded) and Phase 2.5 (partially delivered). Keep: Phase 3.0, Captain's Log ES Backfill

**E. Add to Completed table** — Slice 2: Expansion (2026-03-20), Slice 1: Foundation (2026-03-19)

### 3. `docs/VISION_DOC.md`

The core philosophy (partnership, local sovereignty, transparency, human-first control) is evergreen. But the architectural specifics and file references are stale.

**A. Update the biological mapping table** (lines 65-72)
- Add row: "Prefrontal gateway" → "Pre-LLM Gateway" → "Deterministic filtering before conscious thought"
- Update "Nervous system" → "Orchestrator + Primary Agent" (not just "Orchestrator")
- Add row for delegation: "Social cognition" → "Delegation Hub" → "Knows when to ask for help"

**B. Update "For New AI Assistants" reading order** (lines 311-321)
Current references point to non-existent files. Replace with:
1. `docs/VISION_DOC.md` — This document
2. `README.md` — Project overview
3. `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` — Current architecture
4. `docs/plans/MASTER_PLAN.md` — Current priorities
5. `docs/architecture/` — Historical architecture docs (for context)
6. `docs/architecture_decisions/` — Key decisions and rationale

**C. Update "Architectural Principles" section** (lines 117-176)
- Add principle: **"Deterministic Before Probabilistic"** — aligns with Redesign v2 principle 1 (gateway makes decisions before LLM)
- Add principle: **"Expand and Contract"** — the system breathes: calm → expand for complex tasks → contract when done
- Add principle: **"One Brain, Many Hands"** — single capable model + external delegation, not specialist swarms

**D. Fix stale file references throughout**
- `PROJECT_DIRECTORY_STRUCTURE.md` → doesn't exist, remove or replace with `docs/reference/DIRECTORY_STRUCTURE.md` if it exists
- `ROADMAP.md` → replace with `docs/plans/MASTER_PLAN.md`
- `plans/PROJECT_PLAN_v0.1.md` → replace with `docs/plans/MASTER_PLAN.md`
- `VALIDATION_CHECKLIST.md` → remove (doesn't exist)
- `plans/VELOCITY_TRACKING.md` → exists but likely stale; keep reference or remove

**E. Update version/date** at bottom: v1.1, 2026-03-21

---

## Group B: Superseded Banners (4 architecture docs)

Add a clear superseded banner at the top of each. No other changes — preserve historical record.

### 4. `docs/architecture/COGNITIVE_AGENT_ARCHITECTURE_v0.1.md`
> **SUPERSEDED (2026-03-21)**: This modular cognitive architecture (CognitiveModule protocol, Perception/Planner/Critic/Synthesis/Executor) has been superseded by the [Cognitive Architecture Redesign v2](../specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md) which implements single-brain + gateway + delegation. Retained for historical reference.

### 5. `docs/architecture/ORCHESTRATOR_CORE_SPEC_v0.1.md`
> **SUPERSEDED (2026-03-21)**: This MVP orchestrator spec (role-switching via `resolve_role()`, router/reasoning/coding roles) has been superseded by the [Cognitive Architecture Redesign v2](../specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md). The orchestrator now uses a Pre-LLM Gateway for intent classification and a single primary agent. Retained for historical reference.

### 6. `docs/architecture/INTELLIGENT_ROUTING_PATTERNS_v0.1.md`
> **SUPERSEDED (2026-03-21)**: The hierarchical specialist routing patterns described here (MoMA, LLMRouter) were considered but ultimately rejected. Redesign v2 chose single-brain + delegation over specialist agent routing. See [Cognitive Architecture Redesign v2](../specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md) Section 1.2 for rationale. Retained for historical reference.

### 7. `docs/architecture/ROUTER_SELF_TUNING_ARCHITECTURE_v0.1.md`
> **SUPERSEDED (2026-03-21)**: Router self-tuning is no longer applicable — the router SLM has been removed in Redesign v2 in favor of deterministic intent classification in the Pre-LLM Gateway. See [Cognitive Architecture Redesign v2](../specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md). Retained for historical reference.

---

## Group C: Index & Roadmap Updates (2 files)

### 8. `docs/architecture/README.md`

**A. Add "Current Architecture (Redesign v2)" section** at top (after intro paragraph)
- Link to Redesign v2 spec as primary reference
- Key concepts: Pre-LLM Gateway, single-brain, Seshat MemoryProtocol, expand/contract, delegation
- Links to Slice 1 & 2 plans (complete) and Slice 3 (planned)

**B. Annotate superseded docs** with `(historical)` tags:
- Cognitive Agent Architecture v0.1
- Orchestrator Core Spec v0.1
- Intelligent Routing Patterns v0.1
- Router Self-Tuning Architecture v0.1

### 9. `docs/plans/completed/IMPLEMENTATION_ROADMAP.md`

**A. Add status banner** at top: Phases 2.4/2.5/2.6 superseded by Redesign v2

**B. Update Phase 2.4/2.5/2.6 markers** (around line 26-28):
- Phase 2.4: ✅ Cognitive Architecture Redesign — Slices 1 & 2 complete
- Phase 2.5: 🔄 Partially delivered in Slices 1 & 2, remainder in Slice 3
- Phase 2.6: ✅ Absorbed into Slice 1

---

## Group D: Claude Configuration

### 10. `.claude/CLAUDE.md`

**A. Update Architecture Overview diagram** (~line 220)
- Replace the old service-based ASCII diagram with one reflecting Redesign v2 layers
- Same diagram as README but can keep more detail since this is Claude's reference

**B. Update Core Modules table** (~line 268)
- Add `request_gateway/` — Pre-LLM Gateway (7-stage pipeline)
- Add `insights/` — Cross-data analysis engine
- Add `memory/` — Seshat memory (protocol + service + promotion)
- Update `orchestrator/` — mention sub-agents, HYBRID expansion

**C. Update Phase status** (~line 296)
- Mark Phase 2.4 as superseded by Redesign v2 Slices
- Add: Slices 1 & 2 (Complete), Slice 3 (Planned)

---

## Group E: Spec Status Update (1 file)

### 11. `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md`

**Update status line** (line 4): Change from `**Status**: Draft` to `**Status**: Slices 1 & 2 Implemented — Evaluation Phase (building usage history and traces before Slice 3)`

---

## Out of Scope

- **New files**: No new completion reports — the slice plans are the record
- **Code changes**: Documentation only

## Key Reference Files (read-only)

- `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` — source of truth for architecture
- `docs/superpowers/plans/2026-03-16-slice-1-foundation.md` — Slice 1 plan
- `docs/superpowers/plans/2026-03-18-slice-2-expansion.md` — Slice 2 plan

## Verification

- `git diff --stat` confirms only the 11 target files were modified
- No code, test, or config changes
- All links in updated docs resolve to existing files
- Grep for broken references: `grep -r "PROJECT_DIRECTORY_STRUCTURE\|VALIDATION_CHECKLIST\|PROJECT_PLAN_v0.1" docs/` should return zero hits after update
