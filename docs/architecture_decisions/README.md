# Architecture Decision Records (ADRs)

This directory contains Architecture Decision Records for the Personal Agent (Seshat) project. Each ADR captures a significant architectural decision — the context, the decision itself, and the consequences.

## Index

### Foundation (ADR-0001 – ADR-0007)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0001](ADR-0001-project-init.md) | Initialize Personal Local Agent Project | Accepted |
| [ADR-0002](ADR-0002-orchestrator-style.md) | Orchestrator Style: Deterministic Graph + Embedded Agents | Accepted |
| [ADR-0003](ADR-0003-model-stack.md) | Local Model Stack for Personal Agent MVP | Accepted |
| [ADR-0004](ADR-0004-telemetry-and-metrics.md) | Telemetry & Metrics Implementation Strategy | Accepted |
| [ADR-0005](ADR-0005-governance-config-and-modes.md) | Governance Configuration & Operational Modes | Accepted |
| [ADR-0006](ADR-0006-orchestrator-runtime-structure.md) | Orchestrator Runtime Structure & Execution Model | Accepted |
| [ADR-0007](ADR-0007-unified-configuration-management.md) | Unified Configuration Management | Accepted |

### Tool Calling & Performance (ADR-0008 – ADR-0015)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0008](ADR-0008-hybrid-tool-calling-strategy.md) | Hybrid Tool Calling Strategy for Reasoning Models | Accepted |
| [ADR-0008b](ADR-0008-model-stack-course-correction.md) | Model Stack Course Correction (Dec 2025 Research) | Accepted |
| [ADR-0009](ADR-0009-streaming-vs-non-streaming-responses.md) | Streaming vs Non-Streaming LLM Responses | Accepted |
| [ADR-0010](ADR-0010-structured-llm-outputs-via-pydantic.md) | Structured LLM Outputs via Pydantic Models | Accepted |
| [ADR-0011](ADR-0011-mcp-gateway-integration.md) | MCP Gateway Integration for Tool Expansion | Accepted |
| [ADR-0012](ADR-0012-request-scoped-metrics-monitoring.md) | Request-Scoped Metrics Monitoring | Accepted |
| [ADR-0013](ADR-0013-enhanced-system-health-tool.md) | Enhanced System Health Tool with Historical Queries | Accepted |
| [ADR-0014](ADR-0014-structured-metrics-in-captains-log.md) | Structured Metrics in Captain's Log | Accepted |
| [ADR-0015](ADR-0015-tool-call-performance-optimization.md) | Tool Call Performance Optimization | Accepted |

### Cognitive Architecture (ADR-0016 – ADR-0024)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0016](ADR-0016-service-cognitive-architecture.md) | Service-Based Cognitive Architecture | Accepted |
| [ADR-0017](ADR-0017-multi-agent-orchestration.md) | Multi-Agent Orchestration | Superseded by Redesign v2 |
| [ADR-0018](ADR-0018-seshat-memory-librarian-agent.md) | Seshat Memory Librarian Agent | Partially Delivered (evolved by Redesign v2) |
| [ADR-0019](ADR-0019-development-tracking-system.md) | Development Tracking and Plan Management System | Accepted |
| [ADR-0020](ADR-0020-request-traceability.md) | Request Traceability and Observability | Accepted |
| [ADR-0021](ADR-0021-continuous-metrics-daemon.md) | Continuous Metrics Daemon | Accepted |
| [ADR-0022](ADR-0022-infrastructure-startup-resilience.md) | Infrastructure Startup Resilience and Developer Workflow | Accepted |
| [ADR-0023](ADR-0023-qwen35-model-integration.md) | Qwen3.5 Model Integration — Thinking Control and Response Parsing | Accepted |
| [ADR-0024](ADR-0024-session-graph-model.md) | Session-Centric Graph Model for Behavioral Memory | Accepted (Partial) |

### Memory & Knowledge (ADR-0025 – ADR-0030)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0025](ADR-0025-memory-recall-intent-detection.md) | Memory Recall Intent Detection | Accepted |
| [ADR-0026](ADR-0026-search-memory-native-tool.md) | `search_memory` Native Tool | Accepted |
| [ADR-0027](ADR-0027-memory-cli-interface.md) | Memory CLI Interface | Accepted |
| [ADR-0028](ADR-0028-external-tool-cli-migration.md) | External Tool Integration — CLI-First Migration | Accepted (Implemented) |
| [ADR-0029](ADR-0029-inference-concurrency-control.md) | Inference Concurrency Control (Air Traffic Controller) | Accepted (Implemented) |
| [ADR-0030](ADR-0030-captains-log-dedup-and-self-improvement-pipeline.md) | Captain's Log Deduplication & Self-Improvement Pipeline | Accepted |

### Provider & Model Architecture (ADR-0031 – ADR-0038)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0031](ADR-0031-model-config-consolidation.md) | Model Configuration Consolidation | Accepted |
| [ADR-0032](ADR-0032-robust-tool-calling-strategy.md) | Robust Tool Calling Strategy Across Model Families | Accepted |
| [ADR-0033](ADR-0033-multi-provider-model-taxonomy.md) | Multi-Provider Model Taxonomy, LiteLLM & Delegation Architecture | Accepted (Implemented) |
| [ADR-0034](ADR-0034-searxng-self-hosted-web-search.md) | SearXNG Self-Hosted Web Search Integration | Accepted |
| [ADR-0035](ADR-0035-seshat-backend-decision.md) | Seshat Backend Decision — Neo4j vs Graphiti | Accepted |
| [ADR-0036](ADR-0036-expansion-controller.md) | Expansion Controller — Deterministic Workflow Enforcement | Accepted |
| [ADR-0037](ADR-0037-recall-controller.md) | Recall Controller — Implicit Memory Recall Path | Accepted |
| [ADR-0038](ADR-0038-context-compressor-model.md) | Context Compressor Model Selection | Accepted |

### Self-Improvement & Infrastructure (ADR-0039 – ADR-0042)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0039](ADR-0039-proactive-memory.md) | Proactive Memory via `suggest_relevant()` | Accepted |
| [ADR-0040](ADR-0040-linear-async-feedback-channel.md) | Linear as Async Feedback Channel for Self-Improvement | Accepted (Phases 1–2 Implemented) |
| [ADR-0041](ADR-0041-event-bus-redis-streams.md) | Event Bus via Redis Streams | Accepted (Phases 1–3 Implemented) |
| [ADR-0042](ADR-0042-knowledge-graph-freshness.md) | Knowledge Graph Freshness via Access Tracking | Accepted (Implemented) |

### Orchestrator Governance (ADR-0051 – ADR-0053, ADR-0062)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0051](ADR-0051-cloud-profile-orchestrator-dispatch.md) | Cloud Profile Orchestrator Dispatch via ContextVar | Accepted (Implemented) |
| [ADR-0052](ADR-0052-seshat-owner-identity-primitive.md) | Seshat Owner Identity Primitive | Proposed |
| [ADR-0053](ADR-0053-gate-feedback-monitoring.md) | Deterministic Gate Feedback-Loop Monitoring Framework | Proposed |
| [ADR-0062](ADR-0062-tool-loop-gate.md) | Tool Loop Gate — Per-Tool FSM-Based Loop Detection | Accepted (Implemented) |

### Seshat v2 Architecture (ADR-0043 – ADR-0050)

These ADRs define the next architectural phase: three-layer separation, cloud infrastructure, multi-device UI, and external agent integration.

| ADR | Title | Status | Decision summary |
|-----|-------|--------|-----------------|
| [ADR-0043](ADR-0043-three-layer-separation.md) | Three-Layer Architectural Separation | Accepted | Knowledge / Execution / Observation as distinct layers with explicit ownership boundaries |
| [ADR-0044](ADR-0044-provider-abstraction-dual-harness.md) | Provider Abstraction & Dual-Harness Design | Accepted | Profile-based config for simultaneous local + cloud execution, extending ADR-0033's two-client model |
| [ADR-0045](ADR-0045-infrastructure-cloud-knowledge-layer.md) | Infrastructure — Cloud Knowledge Layer | Accepted | Deploy Knowledge Layer on cloud VM (~$20-40/mo); Terraform + Vault; execution stays flexible |
| [ADR-0046](ADR-0046-agent-to-ui-protocol-stack.md) | Agent-to-UI Protocol Stack | Accepted | AG-UI SSE transport (zero context overhead); terminal + PWA clients; CLI-first preserved |
| [ADR-0047](ADR-0047-context-management-observability.md) | Context Management & Observability | Accepted | Three-tier context model, compaction logging with feedback loops, knowledge freshness and confidence |
| [ADR-0048](ADR-0048-mobile-multi-device-ui.md) | Mobile & Multi-Device UI | Accepted | PWA (Next.js) as primary UI — chat-first, knowledge graph exploration, HITL approval flows |
| [ADR-0049](ADR-0049-application-modularity.md) | Application Modularity | Accepted | Protocol-based module boundaries, dependency injection, swappable components for self-hosting |
| [ADR-0050](ADR-0050-remote-agent-harness-integration.md) | Remote Agent Harness Integration | Accepted | Seshat as MCP server for Claude Code/Codex/Cursor; bidirectional delegation with scoped access |

### Supplementary

| File | Description |
|------|-------------|
| [ADR-0012/0013 Implementation Summary](ADR-0012-0013-IMPLEMENTATION_SUMMARY.md) | Combined implementation notes for metrics monitoring ADRs |

## ADR Lifecycle

- **Proposed** → Under discussion, not yet approved
- **Accepted** → Approved for implementation
- **Implemented** → Code changes complete
- **Superseded** → Replaced by a newer ADR (linked in the document)
- **Deprecated** → No longer applicable

## Conventions

- File naming: `ADR-NNNN-short-description.md`
- Each ADR includes: Status, Date, Deciders, Related/Depends on, Context, Decision, Consequences
- Cross-reference related ADRs by number
- Link to Linear issues where implementation is tracked
