# Architecture Documentation

Living architecture for the Personal Agent lives in **`docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md`** (canonical). It supersedes ADR-0017 (three-tier multi-agent orchestration) and evolves ADR-0018 (Seshat).

**Concepts:** Pre-LLM request gateway (deterministic stages), single primary agent model, Seshat `MemoryProtocol`, expansion/contraction and delegation, brainstem-driven homeostasis.

## Implementation status

| Slice | Plan | Status |
|-------|------|--------|
| Slice 1: Foundation | [`2026-03-16-slice-1-foundation.md`](../superpowers/plans/2026-03-16-slice-1-foundation.md) | Complete |
| Slice 2: Expansion | [`2026-03-18-slice-2-expansion.md`](../superpowers/plans/2026-03-18-slice-2-expansion.md) | Complete |
| Slice 3: Intelligence | Redesign v2 §8.3 | Complete (2026-03-29) |

Next evolution: **`docs/specs/CONTEXT_INTELLIGENCE_SPEC.md`** (active context management, eval follow-up).

## Read next (in this directory)

- **[HOMEOSTASIS_MODEL.md](HOMEOSTASIS_MODEL.md)** — Modes, stability, control loops
- **[HUMAN_SYSTEMS_MAPPING.md](HUMAN_SYSTEMS_MAPPING.md)** — Biological metaphor for subsystems
- **[INSPIRATION_production_grade_system.md](INSPIRATION_production_grade_system.md)** — External reference patterns

The `diagrams/` folder may hold supplemental figures; v0.1 C4 and nervous-system diagrams were moved to **`docs/archive/`** with a consolidated summary in **`docs/archive/PRE_REDESIGN_SUMMARY.md`**.

## Historical material

Pre–Redesign v2 specifications (v0.1 orchestrator, router experiments, older diagrams) are **`docs/archive/`**, not here, to avoid contradicting the current gateway + single-brain design. Use the archive when you need provenance for past decisions.
