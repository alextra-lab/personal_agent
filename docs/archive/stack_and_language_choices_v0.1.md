# Stack and Language Choices (v0.1)

## 1. Context

The production-grade agentic system README assumes a **Python-first stack** with:

- FastAPI + Uvicorn for the web layer.
- LangChain + LangGraph for agent orchestration.
- Structlog, Langfuse, Prometheus, and Grafana for observability.  [oai_citation:14‡README.md](sediment://file_000000005dc4722fb3e02c9bb8f96d47)

Our personal agent shares many architectural *concerns* (orchestration, safety, observability) but targets a **single MacBook Pro** with:

- Local LLM servers (LM Studio, etc.).
- No multi-tenant SaaS gateway.
- Strong emphasis on security, introspection, and learning.

This document explains why we choose **Python** as the primary orchestrator language, and how we plan to structure the stack.

---

## 2. Primary Language: Python (Orchestrator & Governance)

### 2.1 Rationale

We choose Python for:

- Rich ecosystem for agentic frameworks, LLM tooling, and observability.
- Strong alignment with the production-grade reference architecture.
- Fast iteration with AI coding assistants.
- Familiarity.

The orchestrator, Governance & Safety Layer, Tools API, and metric collection will be implemented in Python.

### 2.2 Parallelism Model

Python will focus on **orchestrating IO-bound and tool-bound work**, not heavy numerical kernels:

- Use `asyncio` for concurrent:
  - model calls (to external model servers),
  - web requests,
  - DB queries,
  - background tasks (metrics, logging).
- Use **separate processes** for:
  - system monitoring workers,
  - experiment runners,
  - any CPU-heavy analyses.
- Local LLM servers and vector DB run as **separate processes**, fully utilizing the M4 Max.

This allows the agent to maintain **parallel branches of thought** (e.g. system probe + KB recall + web research), even though the orchestrator itself runs on CPython.

---

## 3. Relationship to LangGraph / LangChain

The production-grade README is tightly integrated with LangGraph and LangChain for graph-based orchestration and tools.  [oai_citation:15‡README.md](sediment://file_000000005dc4722fb3e02c9bb8f96d47)

For the personal agent v0.1:

- We **borrow the architectural ideas**:
  - explicit graphs,
  - nodes for tools/agents,
  - guardrail nodes,
  - quality-control loops.
- We **do not** hard-depend on LangGraph initially:
  - instead, we implement a **minimal graph-of-capabilities** orchestrator tailored to:
    - single-user flows,
    - background monitoring,
    - Captains Log integration.

A future ADR may reconsider a direct LangGraph integration if it adds more value than complexity.

---

## 4. External Services and Other Languages

While Python is the **orchestrator language**, we keep the option open for:

- **Rust / Go** micro-services for:
  - high-throughput log analysis,
  - real-time anomaly detection,
  - specialized monitoring tasks.
- These will be wired into the agent as **tools**:
  - invoked via CLI,
  - or small local HTTP/IPC endpoints.

This keeps the architecture **polyglot where it matters**, without fragmenting the core agent logic.

---

## 5. Next Steps

- Define an initial `pyproject.toml` with:
  - minimal async + logging + DB + test stack.
- Add an ADR:
  - “ADR-0003: Python Orchestrator with External Services for Heavy Compute”.
- Implement a minimal `app/` skeleton:
  - orchestrator module,
  - model-pool client module,
  - tools module,
  - governance module.

This document will be revised as experiments confirm (or contradict) these choices.
