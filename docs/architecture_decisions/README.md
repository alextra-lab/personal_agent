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
| [ADR-0053](ADR-0053-gate-feedback-monitoring.md) | Deterministic Gate Feedback-Loop Monitoring Framework | Parked (scheduled — FRE-582 / FRE-589) |
| [ADR-0062](ADR-0062-tool-loop-gate.md) | Tool Loop Gate — Per-Tool FSM-Based Loop Detection | Accepted (Implemented) |

### Seshat v2 Architecture (ADR-0043 – ADR-0050)

These ADRs define the next architectural phase: three-layer separation, cloud infrastructure, multi-device UI, and external agent integration.

| ADR | Title | Status | Decision summary |
|-----|-------|--------|-----------------|
| [ADR-0043](ADR-0043-three-layer-separation.md) | Three-Layer Architectural Separation | Accepted | Knowledge / Execution / Observation as distinct layers with explicit ownership boundaries |
| [ADR-0044](ADR-0044-provider-abstraction-dual-harness.md) | Provider Abstraction & Dual-Harness Design | Accepted; D1/D2 superseded by ADR-0121 (D3/D4/D5 stand) | Profile-based config for simultaneous local + cloud execution, extending ADR-0033's two-client model |
| [ADR-0045](ADR-0045-infrastructure-cloud-knowledge-layer.md) | Infrastructure — Cloud Knowledge Layer | Accepted | Deploy Knowledge Layer on cloud VM (~$20-40/mo); Terraform + Vault; execution stays flexible |
| [ADR-0046](ADR-0046-agent-to-ui-protocol-stack.md) | Agent-to-UI Protocol Stack | Accepted | AG-UI SSE transport (zero context overhead); terminal + PWA clients; CLI-first preserved |
| [ADR-0047](ADR-0047-context-management-observability.md) | Context Management & Observability | Accepted | Three-tier context model, compaction logging with feedback loops, knowledge freshness and confidence |
| [ADR-0048](ADR-0048-mobile-multi-device-ui.md) | Mobile & Multi-Device UI | Accepted | PWA (Next.js) as primary UI — chat-first, knowledge graph exploration, HITL approval flows |
| [ADR-0049](ADR-0049-application-modularity.md) | Application Modularity | Accepted | Protocol-based module boundaries, dependency injection, swappable components for self-hosting |
| [ADR-0050](ADR-0050-remote-agent-harness-integration.md) | Remote Agent Harness Integration | Accepted | Seshat as MCP server for Claude Code/Codex/Cursor; bidirectional delegation with scoped access |

### Event Bus & Observability Streams (ADR-0054 – ADR-0060)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0054](ADR-0054-feedback-stream-bus-convention.md) | Feedback Stream Bus Convention | Accepted |
| [ADR-0055](ADR-0055-system-health-homeostasis-stream.md) | System Health & Homeostasis Stream | Proposed |
| [ADR-0056](ADR-0056-error-pattern-monitoring.md) | Error Pattern Monitoring Stream | Accepted (Implemented) |
| [ADR-0057](ADR-0057-insights-pattern-analysis.md) | Insights & Pattern Analysis Stream | Accepted (Implemented) |
| [ADR-0058](ADR-0058-self-improvement-pipeline-stream.md) | Self-Improvement Pipeline Stream | Accepted (Implemented) |
| [ADR-0059](ADR-0059-context-quality-stream.md) | Context Quality Stream | Accepted (Implemented) |
| [ADR-0060](ADR-0060-knowledge-graph-quality-stream.md) | Knowledge Graph Quality Stream | Accepted |

### Context, Tools & Governance (ADR-0061 – ADR-0068)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0061](ADR-0061-within-session-progressive-context-compression.md) | Within-Session Progressive Context Compression (head-middle-tail) | Accepted (Implemented) |
| [ADR-0062](ADR-0062-tool-loop-gate.md) | Tool Loop Gate — Per-Tool FSM-Based Loop Detection | Accepted (Implemented) |
| [ADR-0063](ADR-0063-primitive-tools-action-boundary-governance.md) | Primitive Tools & Action-Boundary Governance | Accepted (Implemented) |
| [ADR-0064](ADR-0064-inbound-user-identity-cloudflare-access.md) | Inbound User Identity via Cloudflare Access | Accepted (Implemented) |
| [ADR-0065](ADR-0065-cost-check-gate.md) | Cost Check Gate — Atomic Reservation, Layered Budgets, Retry Telemetry | Accepted |
| [ADR-0066](ADR-0066-skill-routing-defaults-and-feedback-loop.md) | Skill Routing Defaults, Library-Size Threshold, and Missing-Skill Feedback Loop | Accepted |
| [ADR-0067](ADR-0067-reflection-surfacing-in-context-assembly.md) | Reflection Surfacing in Context Assembly | Accepted |
| [ADR-0067b](ADR-0067-skill-nudge-injection.md) | Skill Nudge Injection | Accepted |
| [ADR-0068](ADR-0068-agent-self-telemetry-data-plane.md) | Agent Self-Telemetry Data Plane and Query Interface | Accepted |

### Artifacts & Output (ADR-0069 – ADR-0070)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0069](ADR-0069-r2-backed-artifact-substrate.md) | R2-Backed Artifact Substrate | Implemented |
| [ADR-0070](ADR-0070-output-channel-model-markdown-and-rich.md) | Output Channel Model — Markdown for Agents, Rich for Humans | Implemented |

### Memory & Quality (ADR-0071 – ADR-0074)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0071](ADR-0071-two-source-one-gate-memory-model.md) | Two-Source One-Gate Memory Model | Proposed |
| [ADR-0072](ADR-0072-test-prod-substrate-isolation.md) | Test/Eval Substrate Isolation | Accepted |
| [ADR-0073](ADR-0073-cross-fact-constraint-layer.md) | Cross-Fact Constraint Layer for Memory Pipeline | Proposed |
| [ADR-0074](ADR-0074-end-to-end-traceability.md) | End-to-End Traceability and Observability Joinability | Proposed |

### Transport & Harness Governance (ADR-0075 – ADR-0077)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0075](ADR-0075-websocket-transport.md) | WebSocket Transport + Durable Channel | Implemented (FRE-388, PR #83 + 8 hotfixes) |
| [ADR-0076](ADR-0076-adaptive-constraint-governance.md) | Adaptive Constraint Governance Protocol | Proposed (Codex-reviewed, 3 passes) |
| [ADR-0077](ADR-0077-artifact-draft-subagent-generation.md) | Artifact Draft — Sub-Agent HTML Generation | Implemented (PR #84) |

### Prompt, Profile & Thinking Governance (ADR-0078 – ADR-0081)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0078](ADR-0078-prompt-management-observability.md) | Prompt Management & Observability | Proposed (P0+P1 shipped — FRE-404/405) |
| [ADR-0079](ADR-0079-session-execution-profile-ownership.md) | Server-Authoritative Session Execution Profile | Implemented; subject superseded by ADR-0121 (invariants inherited) |
| [ADR-0080](ADR-0080-thinking-control-policy.md) | Model-Aware Thinking-Control Policy | Implemented (FRE-417, PR #107) |
| [ADR-0081](ADR-0081-cache-aware-context-layout-and-compaction.md) | Cache-Aware Context Layout & Compaction | Proposed |

### Routing, Pedagogy & Memory (ADR-0082 – ADR-0089)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0082](ADR-0082-tier-aware-model-selection-for-single-tasks.md) | Tier-Aware Model Selection for SINGLE-Strategy Tasks | Proposed (partially superseded by ADR-0084) |
| [ADR-0083](ADR-0083-adaptive-limits-and-error-recovery.md) | Adaptive Limits & Error Recovery: Layer 3 SLM Health Observability | Proposed |
| [ADR-0084](ADR-0084-pedagogical-architecture-socratic-tutor-layer.md) | Pedagogical Architecture: Socratic Tutor Layer, Result Type Taxonomy, Delegation Policy | Accepted |
| [ADR-0085](ADR-0085-intra-turn-tool-result-compression.md) | Intra-Turn Tool-Result Compression (Insertion-Time Digest + Exact Re-Expand) | Parked (dormant, flag-off) |
| [ADR-0086](ADR-0086-hybrid-decompose-routing-for-artifact-builds.md) | HYBRID/DECOMPOSE Routing for High-Complexity Artifact Builds | Proposed |
| [ADR-0087](ADR-0087-memory-recall-quality-measurement-program.md) | Memory-Recall Quality: A Measurement-First Program (Diagnose → Gate → Architecture) | Proposed |
| [ADR-0088](ADR-0088-execution-topology-observability-contract.md) | Execution Topology Observability Contract (Trace-Scoped Spine for Status, Cost, Loud Degradation) | Accepted |
| [ADR-0089](ADR-0089-artifact-execution-security-model.md) | Artifact Execution Security Model (Sandbox the Execution, Don't Sanitize the Output) | Implemented (supersedes ADR-0070 D7 + FRE-500) |
| [ADR-0090](ADR-0090-telemetry-surface-contract.md) | Telemetry Surface Contract (Emit ↔ Mapping ↔ Dashboard Reconciliation) | Accepted |
| [ADR-0091](ADR-0091-eval-conversation-driver-and-completion-status-layer.md) | Eval Conversation Driver & Turn Completion-Status Layer | Accepted |
| [ADR-0092](ADR-0092-context-compaction-observability-and-surfacing.md) | Context-Compaction Observability & Surfacing | Implemented |
| [ADR-0093](ADR-0093-opentelemetry-boundary-migration.md) | OpenTelemetry at the Substrate Boundary | Accepted (scoped) |
| [ADR-0094](ADR-0094-deterministic-local-cloud-execution-profile-routing.md) | Deterministic Local/Cloud Execution-Profile Routing | Proposed |
| [ADR-0095](ADR-0095-delegation-boundary-per-worker-routing-and-grammar.md) | Delegation Boundary: Per-Worker Routing + Grammar-Constrained Sub-Agent Output | Proposed |
| [ADR-0096](ADR-0096-memory-access-model-coordinated-hybrid.md) | Memory Access Model: Coordinated Hybrid (Ambient Floor + On-Demand Retrieval) | Accepted |
| [ADR-0097](ADR-0097-ingested-knowledge-taxonomy.md) | Ingested-Knowledge Taxonomy (hypothesis) | Proposed (supersedes ADR-0071) |
| [ADR-0098](ADR-0098-memory-substrate-and-lifecycle-architecture.md) | Memory Substrate & Lifecycle Architecture (Core/Docs topology; living-knowledge model) | Accepted |
| [ADR-0099](ADR-0099-configuration-management-and-validation.md) | Configuration Management & Validation (single-source role matrix + validator) | Accepted; amended by ADR-0121 |
| [ADR-0100](ADR-0100-relevance-bounded-recall.md) | Memory Recall — Relevance-Bounded Candidate Generation | Accepted |
| [ADR-0101](ADR-0101-agent-vision-ingestion.md) | Agent Vision Ingestion of Uploaded Images | Accepted |
| [ADR-0102](ADR-0102-document-ingestion.md) | Document Ingestion (PDF) — tiered, capability-routed | Accepted |
| [ADR-0103](ADR-0103-recall-no-clean-floor-structural-separation.md) | Recall is Retrieval — No Clean Similarity Floor; Separation is Structural | Accepted |
| [ADR-0104](ADR-0104-multi-path-retrieval-rank-fusion.md) | Multi-Path Retrieval with Rank Fusion | Proposed |
| [ADR-0105](ADR-0105-convergent-self-improvement-pipeline-and-system-graph.md) | Convergent Self-Improvement Pipeline & Isolated System Graph | Accepted |
| [ADR-0106](ADR-0106-system-user-knowledge-boundary-dispatch-observe-ground.md) | The System/User Knowledge Boundary — Dispatch by Output Kind, Observe, Ground | Accepted |
| [ADR-0107](ADR-0107-user-identity-resolution-and-log-propagation.md) | User Identity Resolution for Claims + Trace/Log Identity Propagation | Accepted |
| [ADR-0108](ADR-0108-stored-artifact-vision-reprocessing.md) | Stored-Artifact Vision Re-processing (analyze-to-text, explicit tool) | Proposed |
| [ADR-0109](ADR-0109-entity-taxonomy-redesign.md) | Entity & Relationship Taxonomy — V1 (inherited) → V2 (first principled derivation) | Accepted |

### Dispatch, Delivery Process & Substrate (ADR-0110 – ADR-0122)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0110](ADR-0110-external-dispatch-orchestrator.md) | External Dispatch Orchestrator for build/adr Worker Sessions | Proposed (transport half superseded by ADR-0116) |
| [ADR-0111](ADR-0111-infrastructure-topology-and-data-custody.md) | Infrastructure Topology & Data-Custody Policy | Superseded by ADR-0112 |
| [ADR-0112](ADR-0112-configurable-substrate-backends.md) | Configurable Substrate Backends — Owner-Controlled Storage by Default | Accepted |
| [ADR-0113](ADR-0113-self-driving-delivery-loop.md) | Self-Driving Delivery Loop — Autonomous Actuation, Human-Gated Judgment | Superseded |
| [ADR-0114](ADR-0114-heterarchical-associative-memory-study.md) | Heterarchical Associative Memory — Decoupled Research Study | Proposed |
| [ADR-0115](ADR-0115-knowledge-class-axis-emission-persistence-dispatch.md) | The Knowledge Class Axis — Two-Axis Emission, Persistence, Dispatch | Implemented |
| [ADR-0116](ADR-0116-event-driven-dispatch-actuation.md) | Event-Driven Dispatch Actuation (capability-gateway + MCP Channels) | Accepted |
| [ADR-0117](ADR-0117-pr-gate-signal-collector.md) | Deterministic Signal Collector for the PR Gate | Accepted |
| [ADR-0118](ADR-0118-artifact-builder-model-selection.md) | Model-Selection Layer for Open Roles — User-Selectable Artifact Builder (Phase 1) | Superseded by ADR-0121 + ADR-0122 |
| [ADR-0119](ADR-0119-config-management-interface.md) | Config-Management Interface (Phase 1) — Observe + Open-Role Model Selection | Superseded by ADR-0121 |
| [ADR-0120](ADR-0120-cost-governance-visibility-consent.md) | Cost Governance — Visibility + Consent (supersedes ADR-0065) | Proposed |
| [ADR-0121](ADR-0121-model-catalog-and-selection-layer.md) | Model Catalog and Selection Layer — Providers, Deployments, Bindings; the User Selects the Model | Accepted |
| [ADR-0122](ADR-0122-build-time-artifact-builder-selection.md) | Per-Build Artifact Builder Selection — Choose the Model Before the Plan Is Written | Accepted (amended 2026-07-21 — card at turn start) |

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
