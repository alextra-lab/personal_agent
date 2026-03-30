# Pre-Redesign Architecture Summary (Phases 1.0 – 2.2)

> **ARCHIVED** — Consolidated from v0.1 architecture documents. Current architecture: [`docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md`](../specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md)

**Date consolidated:** 2026-03-30

## Key Decisions That Carried Forward

- **Local-first, sovereign runtime:** macOS-focused deployment, offline-capable core reasoning, governed outbound access (`system_architecture_v0.1.md`).
- **Python orchestrator:** asyncio for I/O-bound work, separate processes for heavy work; structlog/structured telemetry (`stack_and_language_choices_v0.1.md`).
- **Deterministic governance layer:** supervisor, outbound gatekeeping, auditable behavior — evolved into ADR-governed modes and policies.
- **Multi-store memory concept:** working / episodic / semantic framing informed later Seshat/Neo4j work (not identical to v0.1 module layout).
- **Observability:** trace-oriented logging, metrics, evaluation hooks — retained and expanded in Slice 2–3 telemetry.
- **C4-style diagrams:** container/context views were useful communication artifacts; current docs use Redesign v2 specs as source of truth.

## Key Decisions That Were Superseded

| v0.1 / router-era idea | Current direction | Pointer |
|------------------------|-------------------|---------|
| Modular **Planner / Critic / Perception** cognitive modules with `CognitiveModule` protocol | **Single primary agent + request gateway** + delegation where needed | [`COGNITIVE_ARCHITECTURE_REDESIGN_v2.md`](../specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md), ADR-0036 area |
| **Multi-tier intent router** (E-005–E-007 experiments, intelligent routing patterns) | Gateway classification + orchestrator expansion; router-specific tuning archived | Archived experiment docs in this folder |
| **Brainstem** as separate v0.1 service spec | Capabilities folded into orchestrator/brainstem modes per Redesign v2 | `brainstem/` code + specs |
| **Request monitor** as primary integration surface | Telemetry + evaluation harness + gateway pipeline stages | `telemetry/`, `request_gateway/` |
| LangGraph/LangChain as **default** orchestration spine | Custom state machine + explicit pipelines (Slice 1–3) | Implementation in `src/personal_agent/orchestrator/` |

## Historical Context

1. **Phase 1.0 (late 2025):** System architecture and stack choices established; cognitive agent architecture described modular neuroscience-inspired components.
2. **Phase 2.1–2.2:** Orchestrator specs, routing experiments, and service-level docs iterated; evaluation and router experiments documented under `architecture/experiments` and `architecture_decisions/experiments`.
3. **Redesign trigger:** Operational complexity and eval gaps (routing vs. single-brain clarity, maintainability) led to **Cognitive Architecture Redesign v2** (single brain, gateway, explicit stages).
4. **Post–Redesign v2:** Slices 1–3 delivered foundation, expansion/decomposition, and gateway intelligence; context management is tracked in `CONTEXT_INTELLIGENCE_SPEC.md`.

## Archived Sources

Individual v0.1 markdown files, router experiments, old ADR-adjacent snapshots, and pre–March 2026 session logs were moved alongside this summary under `docs/archive/`. For authoritative decisions, use numbered ADRs (`docs/architecture_decisions/ADR-*.md`) and Redesign v2 specs.
