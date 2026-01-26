# Inspiration from “Production-Grade Agentic AI System”

This document records how the **Personal Local AI Collaborator** takes inspiration from the 7-layer “Production-Grade Agentic AI System” architecture, and which ideas are **adopted, adapted, postponed, or rejected** for this single-user, local-first agent.

The reference system is built as multiple architectural layers:

- Creating a **modular codebase** with clear folders like `app/`, `tools/`, `langgraph/`, `evals/`, `grafana/`, `prometheus/`, and CI under `.github/`.  [oai_citation:0‡README.md](sediment://file_000000005dc4722fb3e02c9bb8f96d47)
- A **data persistence layer** with structured entities, DTOs, and DB models.  [oai_citation:1‡README.md](sediment://file_000000005dc4722fb3e02c9bb8f96d47)
- A **security & safeguards layer** with rate limiting, sanitization logic, and context management.  [oai_citation:2‡README.md](sediment://file_000000005dc4722fb3e02c9bb8f96d47)
- A **service layer for AI agents** adding connection pooling, LLM unavailability handling, and circuit breakers.  [oai_citation:3‡README.md](sediment://file_000000005dc4722fb3e02c9bb8f96d47)
- A **multi-agentic architecture** with long-term memory and tool-calling.  [oai_citation:4‡README.md](sediment://file_000000005dc4722fb3e02c9bb8f96d47)
- An **API gateway** for auth and real-time streaming endpoints.  [oai_citation:5‡README.md](sediment://file_000000005dc4722fb3e02c9bb8f96d47)
- **Observability & operational testing** using Prometheus + Grafana + an evaluation framework.  [oai_citation:6‡README.md](sediment://file_000000005dc4722fb3e02c9bb8f96d47)

---

## 1. Mapping: Production System → Personal Agent

| 7-Layer Concept                      | Personal Agent Equivalent (v0.1)                                        |
|-------------------------------------|-------------------------------------------------------------------------|
| Modular codebase (`app/`, `tools/`) | `app/` (orchestrator + services), `tools/` (capability services)       |
| Data persistence layer              | KB & World Model + Metrics & Evaluation Collector                      |
| Security & safeguards layer         | Governance & Safety Layer (Supervisor, Outbound Gatekeeper, policies)  |
| Service layer for AI agents         | Local Model Pool + Tool API layer (timeouts, retries, fallback)        |
| Multi-agentic architecture          | Graph-of-capabilities with optional sub-agents / “parallel thoughts”   |
| API gateway                         | Local UI + localhost API (single-user, no external exposure)           |
| Observability & evaluation          | Metrics DB + Captains Log + evaluation experiments                     |

The **core ideas align strongly** with `system_architecture_v0.1.md`; this doc makes that relationship explicit.  [oai_citation:7‡orchestration-survey.md](sediment://file_000000001844722fb5cbc1fde1c2e719)

---

## 2. What We Explicitly Adopt (Now)

### 2.1 Layered mental model

We adopt the idea that the system is a **set of layers with clear responsibilities**, not a monolith:

- Modular codebase structure
- Persistence for knowledge, state, and metrics
- Security & safeguards as a **separate concern**
- Service layer for LLM / tools
- Observability and evaluation as first-class features

This is already reflected in the following components:

- Core Orchestrator
- Local Model Pool
- Tools & Capability Services
- Knowledge Base & World Model
- Governance & Safety Layer
- Metrics & Evaluation Collector

### 2.2 Dependency & environment discipline

From the README’s `pyproject.toml` section, we adopt the **discipline**, not necessarily all libraries:  [oai_citation:8‡README.md](sediment://file_000000005dc4722fb3e02c9bb8f96d47)

- Use `pyproject.toml` instead of ad-hoc `requirements.txt`.
- Separate **core runtime deps** from **dev/test/observability deps**.
- Treat environment configuration as a first-class concern.

This will be captured later in `pyproject.toml` and an ADR for stack & language.

### 2.3 Service-layer patterns for the Local Model Pool

We adopt the **service-layer thinking** used for LLM handling: connection pooling, unavailability handling, circuit breaking.  [oai_citation:9‡README.md](sediment://file_000000005dc4722fb3e02c9bb8f96d47)

For the **Local Model Pool**, this means:

- Requests to local models are always made through a **small client library** that:
  - enforces **timeouts**,
  - does **retries with backoff**,
  - can **fail over** to an alternative model when one is unavailable.
- The Orchestrator never talks to model servers “raw”; it always goes through this service layer.

### 2.4 Security & safeguards as their own layer

We mirror the **Security & Safeguards** section (rate limiting, sanitization, context management) into our **Governance & Safety Layer**:  [oai_citation:10‡README.md](sediment://file_000000005dc4722fb3e02c9bb8f96d47)

- Rate limiting:
  - limit the frequency of **tool invocations** and **web requests**.
- Sanitization:
  - centralize all outbound content checks in the **Outbound Gatekeeper**.
- Context management:
  - manage context budgets in the Orchestrator, with policies for truncation, summarization, and refusal.

### 2.5 Observability & evaluation mindset

We adopt the “**observability + eval as core**” stance:

- Keep a place for metrics + traces (later, exporters to Prometheus/Grafana if useful).  [oai_citation:11‡README.md](sediment://file_000000005dc4722fb3e02c9bb8f96d47)
- Maintain an `evals/` or `governance/experiments/` folder as the home for:
  - prompt experiments,
  - agent-behavior tests,
  - regression checks on reasoning patterns.

---

## 3. What We Adapt (Scaled Down for Single User)

### 3.1 API Gateway

The production system introduces a full API gateway with auth endpoints and real-time streaming.  [oai_citation:12‡README.md](sediment://file_000000005dc4722fb3e02c9bb8f96d47)

For the personal agent:

- **We do not build** a multi-tenant HTTP gateway initially.
- Instead:
  - provide a **local UI** (CLI / desktop),
  - plus a minimal **localhost API** for:
    - `/conversation`
    - `/task`
    - `/metrics` (read-only)
- Auth is simply “local user on this Mac”, hardened by:
  - dedicated macOS user account,
  - sandboxing.

### 3.2 Prometheus + Grafana stack

The reference architecture uses Prometheus + Grafana for metrics and dashboards.  [oai_citation:13‡README.md](sediment://file_000000005dc4722fb3e02c9bb8f96d47)

For the personal agent v0.1:

- We **log OpenTelemetry-style data locally** (e.g. SQLite + structured logs).
- We **leave the door open** to:
  - optional Prometheus + Grafana
  - or a lightweight local dashboard
- This is deferred to a future ADR once the core agent is useful.

---

## 4. What We Explicitly Postpone or Reject (for Now)

- **Multi-tenant SaaS assumptions** (10k users, JWT auth, full gateway hardening).
- **Full Dockerized infra stack** (Postgres pgvector, Prometheus, Grafana, app containers) as a baseline requirement.
- **Tight coupling to LangGraph / LangChain**:
  - We treat them as **reference designs**, not mandatory dependencies.
  - The Orchestrator will implement a **graph-of-capabilities** with similar semantics, but hand-rolled and focused on local needs.

These decisions keep the project **simple enough to learn from** while maintaining conceptual alignment with production-grade architectures.

---

## 5. Summary

The production-grade 7-layer architecture is treated as a **north star**:

- We adopt its **layered thinking**, **security mindset**, and **observability discipline**.
- We adapt its **service layer** and **modular structure** to a single-user, local-first context.
- We postpone multi-tenant gateway complexity and heavy infra until (and unless) the personal agent evolves into a shared or enterprise security monitoring platform.

Future ADRs can revisit this document when the personal agent grows beyond a single Mac.
