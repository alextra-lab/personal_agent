# Personal Local IT Assistant — Functional Specification v0.1

## 1. Mission

A sovereign, local-first AI partner that acts as a research collaborator, technical advisor, and systems assistant. It should challenge assumptions, co-develop ideas, operate safely within strict governance boundaries, and continuously improve its reasoning and internal understanding while remaining fully transparent, auditable, and accountable.

This mission is governed and constrained by the identity, reasoning discipline, and safety philosophy defined in `../architecture_decisions/AGENT_IDENTITY.md`.

## 2. Core Roles

- Strategic Research Partner (questions, challenges, synthesizes knowledge)
- Coding & Architecture Assistant (critical reasoning, refactoring, structured thinking)
- System Health & Security Advisor (observes, explains, recommends)
- Self-Reflective Agent (maintains Captains Log, self-analysis, structured introspection)
- Safe & Explainable Thinker (explicit uncertainty, traceable reasoning, non-silent optimization)

## 3. Primary Capabilities (Phase 1)

- Deep reasoning using local LLMs
- Architecture reasoning + code generation (multi-language, but Python-focused initially)
- macOS system observation + interpretable health/security recommendations
- Web-augmented reasoning with outbound policy review
- Structured self-questioning after tasks and periodic meta-reflection
- Captains Log: structured internal thoughts, improvement proposals, and knowledge tracking
- Metrics + telemetry for agent performance, decisions, tool-effectiveness
- Background monitoring with governance boundaries

### 3.1 Interaction & UI (MVP)

- Primary UI: **terminal-based conversational interface** (CLI / TUI) running locally on the Mac.
- Interaction style: multi-turn natural language conversation, initially optimized for **English**.
- Commands and controls (e.g., `/status`, `/modes`, `/logs`) are available as structured prompts on top of natural language.
- Future frontends (web UI, GUI, editor integrations) are out of scope for Phase 1 but must be enabled by clean APIs and process boundaries.

## 4. Non-Functional Requirements

- Local-first by design (no cloud dependency for core reasoning)
- Strong transparency: internal thinking, logs, and memory must be inspectable
- Deterministic accountability: every meaningful action traceable
- Secure-by-default: constrained, sandboxed, least-privileged execution
- Evolvable but disciplined: changes require governance review
- Human-partner mindset: supports exploration, not silent automation

## 5. Autonomy Boundaries

- Can propose config changes (requires human approval)
- Cannot install tools/models
- Can schedule background tasks
- Restricted filesystem + shell permissions
- May NOT silently alter its own reasoning structure or world model without logging justification

All autonomy expectations are also defined and reinforced by the identity principles documented in `../architecture_decisions/AGENT_IDENTITY.md`, including:

- Human-first control
- Explainable plans before action
- Explicit consent for risky or impactful behavior

## 6. Security Posture

- Runs under a restricted macOS user with minimized entitlements
- Sandboxed execution for risky tools (Docker/macOS sandbox hybrid)
- Deterministic Supervisor able to kill agent processes
- Outbound Gatekeeper that inspects text and applies policy, cannot be prompted by the agent

Security posture is not only technical but behavioral, and is explicitly aligned with the principles outlined in `../architecture_decisions/AGENT_IDENTITY.md` (safety over cleverness, context stewardship, disciplined intelligence).

## 7. Governance Model

Governance behavior must remain consistent with the values and constraints defined in `../architecture_decisions/AGENT_IDENTITY.md`.

- Captains Log as structured evolving introspection journal (git versioned)
- Governance repository separate from execution
- PR-style configuration proposals with human approval workflow
- ADR-driven evolution of design decisions

## 8. Evaluation & Telemetry Philosophy

- Agent provides self-score with justification
- Human can override with structured feedback
- Both stored and analyzable for scientific learning
- Supports experiments (A/B config, model routing behavior, etc.)

Evaluation is not only functional performance; it measures identity alignment, safety discipline, and partnership quality as defined in `../architecture_decisions/AGENT_IDENTITY.md`.

## 9. MVP Scope Boundary

### Phase 1 WILL Deliver

- Reliable local LLM-driven reasoning and coding assistance
- Deterministic and explainable system observation + recommendations
- Operational Captains Log with structured self-analysis + git commit behavior
- Supervisor + Outbound Gatekeeper foundations
- Metrics collection and ability to analyze trends
- Background monitoring within defined safety constraints
- Behavioral alignment with the principles defined in `../architecture_decisions/AGENT_IDENTITY.md`

### Phase 1 WILL NOT

- Execute destructive system changes autonomously
- Install new tools, models, or modify system environment
- Fully automate self-improvement loops (proposal-only, not execution)
- Act as a general-purpose chatbot without constraints
- Replace human judgment — it collaborates, not commands

## 10. Open Questions

- Final model stack selection
- Orchestration architecture (single brain vs structured multi-agent)
- Exact knowledge base data structures and world-model representation
- How often should reflection run by default?
- Where is the correct line between “assistant” and “autonomous collaborator”?

## 11. Foundational Technical Assumptions (MVP Draft)

The following assumptions are **technical enablers** for Phase 1 and are captured more formally in Architecture Decision Records (ADRs). They do not expand the product scope, but constrain how the system will be built.

- **Implementation language & runtime**
  - Core agent implemented in **Python 3.12**, using a `src/` layout and modern tooling (uv, pytest, mypy, ruff).
  - Command-line entrypoints provide the initial interaction surface.

- **Local LLM stack (models + serving)**
  - The agent relies on a **local model stack** (coding + reasoning + small routing/utility models) served by external processes.
  - Functional spec does **not** mandate specific models, but requires:
    - offline capability,
    - good coding assistance,
    - strong reasoning for research and system analysis.
  - Concrete choices and serving strategy will be defined in a dedicated ADR (e.g., `architecture_decisions/ADR-0003-model-stack.md`).

- **Storage & state**
  - The agent maintains local, inspectable state for:
    - Captains Log (git-versioned, human-readable documents),
    - telemetry and metrics (structured logs / lightweight store),
    - configuration and governance policies (files and/or small local DB).
  - No remote or cloud storage is required for core behavior.

- **Telemetry & observability**
  - All meaningful actions, mode transitions, and reflexes must emit structured logs/metrics.
  - A minimal observability stack (e.g., structured logs compatible with OpenTelemetry-style processing) is required for Phase 1.
  - Details (storage backend, formats, retention) will be specified in a dedicated ADR (e.g., `architecture_decisions/ADR-0004-telemetry-and-metrics.md`).

- **Governance & policy representation**
  - Runtime governance (modes, permissions, tool policies) is represented as **explicit configuration** separate from code.
  - The Brainstem reads and enforces these policies at runtime.
  - Policy formats and lifecycle will be defined in a governance-related ADR (e.g., `architecture_decisions/ADR-0005-governance-config-and-modes.md`).

These assumptions ensure that when the MVP is implemented, it remains faithful to the mission and governance constraints, while leaving detailed technology choices to specific ADRs rather than overloading the functional specification.
