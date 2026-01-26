# ADR-0002 — Orchestrator Style: Deterministic Graph + Embedded Agents

**Status:** Proposed / Draft
**Date:** 2025-12-28
**Decision Owner:** Project Owner

---

## 1. Context

The Personal Local AI Collaborator requires an orchestration approach that:

- Supports complex, multi-step reasoning and execution.
- Allows **parallel branches of thought** (planner vs critic vs researcher).
- Remains **observable, auditable, and deterministic** for safety and governance.
- Can evolve from an MVP into a sophisticated personal assistant capable of:
  - coding and development support,
  - local system monitoring and analysis,
  - web-augmented reasoning,
  - self-evaluation and self-improvement proposals.

Multiple architectures were considered, informed by deep research into:

- LangGraph and graph/state-machine orchestrators.
- Conversational multi-agent systems such as AutoGen.
- Hybrid supervisory systems used in industry research and modern agentic frameworks.

There is a growing consensus in academic and practitioner literature that:

> Use **deterministic graphs / state machines for control**, and **LLM agents for cognition** inside bounded steps.

This aligns strongly with the architectural goals of safety, introspection, and transparency.

---

## 2. Decision

We adopt a **hybrid orchestration model**:

### 2.1 Deterministic Graph / State Machine as the Primary Control Structure

The Core Orchestrator will:

- Execute workflows using an explicit **graph of nodes**.
- Each node represents:
  - a task phase,
  - a tool invocation,
  - a reasoning step,
  - a validation/safety checkpoint,
  - or a parallel execution branch.
- Transitions between nodes are explicit and logged.
- State is materialized and resumable.

This enables:

- Determinism,
- Debuggability,
- Reproducibility,
- Auditability.

---

### 2.2 Embedded Agent Cognition Within Nodes

Within specific nodes, one or more **LLM agents** will be invoked to perform:

- planning,
- synthesis,
- self-critique,
- option evaluation,
- exploratory reasoning.

In certain workflows, multiple agents may run in *parallel branches of thought* and converge at a synthesis node.

This preserves flexibility, intelligence, emergent reasoning, and creativity — *inside controlled boundaries*.

---

## 3. Rationale

### Why Not Fully Conversational Multi-Agent?

While conversational multi-agent systems excel at emergent reasoning and collaboration, they:

- are harder to audit,
- are less deterministic,
- complicate governance and safety enforcement,
- can drift or loop without strong structural constraints.

These risks are unacceptable for a system that:

- runs locally,
- touches personal data,
- executes tools with system impact,
- may propose self-modification.

A free-form agent talking to itself is powerful — but needs rails.

---

### Why Not Only Deterministic Graphs?

Pure graph systems can feel rigid and over-engineered when tasks are:

- exploratory,
- research-driven,
- knowledge intensive,
- ambiguous by nature.

A colleague-like AI assistant must be able to:

- reason,
- hypothesize,
- debate with itself,
- change course intelligently.

Thus, cognition must remain flexible, adaptive, and agentic.

---

### Why Hybrid Works Best

This approach gives us:

**Deterministic Control**

- predictable execution
- safe handoffs
- explicit checkpoints
- easy logging
- clear state

**Intelligent Cognition**

- reasoning agents
- debate
- self-questioning
- improvement proposals

**Security / Governance Fit**

- natural enforcement points
- supervisor integration
- outbound policy checks
- human-in-the-loop approval where required

---

## 4. Implementation Implications

- Core Orchestrator will implement a **graph-of-capabilities** execution engine.
- Graph nodes will support:
  - single execution steps,
  - parallel execution branches,
  - guardrail/safety checkpoints,
  - agent-reasoning steps.
- Background monitoring and long-running workflows will also be managed by this orchestrator model.
- Observability will treat each node as a traceable span.
- Metrics and decisions will be logged to support introspection and evaluation.
- Future extensions such as:
  - multi-agent collaboration,
  - nested subgraphs,
  - richer planning cycles,
  can be added without redesigning the system.

---

## 5. Alternatives Considered

### A) Fully Conversational Multi-Agent

Rejected for MVP due to:

- safety concerns,
- lack of deterministic control,
- difficulty guaranteeing predictable behavior.

### B) Strict Workflow / DAG System

Rejected because:

- too rigid for exploratory cognitive tasks,
- compromises flexibility and adaptability.

### C) Rule-Based Orchestration Only

Rejected:

- insufficient intelligence,
- too brittle,
- does not meet project ambition.

---

## 6. Consequences

### Positive

- Strong governance and transparency.
- Safer execution.
- Easier debugging and reasoning.
- Supports “parallel branches of thought.”
- Evolves naturally toward more sophisticated agentic systems.

### Negative

- Requires more engineering than a simple conversational loop.
- Some upfront cost in designing graph semantics.
- More components to reason about conceptually.

---

## 7. References

- Orchestration research (graph vs conversational systems).
- Architecture inspiration from production-grade multi-layer agent systems.
- Internal documents:
  - system_architecture_v0.1.md
  - INSPIRATION_production_grade_system.md

---

## 8. Status and Next Steps

- This ADR becomes binding once MVP orchestrator begins.
- Future ADRs may refine:
  - execution semantics,
  - graph persistence format,
  - agent role taxonomy,
  - failure recovery strategy.
