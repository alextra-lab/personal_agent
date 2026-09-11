# Architecture Decision Records (ADRs)

This directory contains Architecture Decision Records for the Personal Agent (Seshat) project. Each ADR captures a significant architectural decision — the context, the decision itself, and the consequences.

## Index

### Foundation (ADR-0001 – ADR-0007)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0001](ADR-0001-project-init.md) | Initialize Personal Local Agent Project | Accepted |
| [ADR-0002](ADR-0002-orchestrator-style.md) | Orchestrator Style: Deterministic Graph + Embedded Agents | Accepted |
| [ADR-0003](ADR-0003-model-stack.md) | Local Model Stack for Personal Agent MVP | Superseded by ADR-0008 / ADR-0031 / ADR-0033 + Redesign v2 |
| [ADR-0004](ADR-0004-telemetry-and-metrics.md) | Telemetry & Metrics Implementation Strategy | Accepted |
| [ADR-0005](ADR-0005-governance-config-and-modes.md) | Governance Configuration & Operational Modes | Accepted |
| [ADR-0006](ADR-0006-orchestrator-runtime-structure.md) | Orchestrator Runtime Structure & Execution Model | Accepted |
| [ADR-0007](ADR-0007-unified-configuration-management.md) | Unified Configuration Management | Accepted |

### Tool Calling & Performance (ADR-0008 – ADR-0015)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0008](ADR-0008-hybrid-tool-calling-strategy.md) | Hybrid Tool Calling Strategy for Reasoning Models | Accepted |
| [ADR-0008b](ADR-0008-model-stack-course-correction.md) | Model Stack Course Correction (Dec 2025 Research) | Superseded by Cognitive Architecture Redesign v2 |
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
| [ADR-0037](ADR-0037-recall-controller.md) | Recall Controller — Implicit Memory Recall Path | Superseded by FRE-1135 (2026-08-28) |
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
| [ADR-0052](ADR-0052-seshat-owner-identity-primitive.md) | Seshat Owner Identity Primitive | Accepted (amended 2026-05-09) |
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
| [ADR-0060](ADR-0060-knowledge-graph-quality-stream.md) | Knowledge Graph Quality Stream | Superseded (2026-07-02) |

### Context, Tools & Governance (ADR-0061 – ADR-0068)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0061](ADR-0061-within-session-progressive-context-compression.md) | Within-Session Progressive Context Compression (head-middle-tail) | Accepted (Implemented) |
| [ADR-0062](ADR-0062-tool-loop-gate.md) | Tool Loop Gate — Per-Tool FSM-Based Loop Detection | Accepted (Implemented) |
| [ADR-0063](ADR-0063-primitive-tools-action-boundary-governance.md) | Primitive Tools & Action-Boundary Governance | Accepted (Implemented) |
| [ADR-0064](ADR-0064-inbound-user-identity-cloudflare-access.md) | Inbound User Identity via Cloudflare Access | Accepted (Implemented) |
| [ADR-0065](ADR-0065-cost-check-gate.md) | Cost Check Gate — Atomic Reservation, Layered Budgets, Retry Telemetry | Superseded by ADR-0120 (2026-07-16) |
| [ADR-0066](ADR-0066-skill-routing-defaults-and-feedback-loop.md) | Skill Routing Defaults, Library-Size Threshold, and Missing-Skill Feedback Loop | Accepted |
| [ADR-0067](ADR-0067-reflection-surfacing-in-context-assembly.md) | Reflection Surfacing in Context Assembly | Superseded |
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
| [ADR-0071](ADR-0071-two-source-one-gate-memory-model.md) | Two-Source One-Gate Memory Model | Superseded by ADR-0097 + ADR-0098 |
| [ADR-0072](ADR-0072-test-prod-substrate-isolation.md) | Test/Eval Substrate Isolation | Accepted |
| [ADR-0073](ADR-0073-cross-fact-constraint-layer.md) | Cross-Fact Constraint Layer for Memory Pipeline | Implemented (FRE-374) |
| [ADR-0074](ADR-0074-end-to-end-traceability.md) | End-to-End Traceability and Observability Joinability | Accepted |

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
| [ADR-0081](ADR-0081-cache-aware-context-layout-and-compaction.md) | Cache-Aware Context Layout & Compaction | Implemented — 2026-05-29 (flag retired 2026-07-22, FRE-941) |

### Routing, Pedagogy & Memory (ADR-0082 – ADR-0089)

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0082](ADR-0082-tier-aware-model-selection-for-single-tasks.md) | Tier-Aware Model Selection for SINGLE-Strategy Tasks | Partially superseded by ADR-0084 (D1 plumbing stands) |
| [ADR-0083](ADR-0083-adaptive-limits-and-error-recovery.md) | Adaptive Limits & Error Recovery: Layer 3 SLM Health Observability | Accepted |
| [ADR-0084](ADR-0084-pedagogical-architecture-socratic-tutor-layer.md) | Pedagogical Architecture: Socratic Tutor Layer, Result Type Taxonomy, Delegation Policy | Accepted |
| [ADR-0085](ADR-0085-intra-turn-tool-result-compression.md) | Intra-Turn Tool-Result Compression (Insertion-Time Digest + Exact Re-Expand) | Parked (dormant, flag-off) |
| [ADR-0086](ADR-0086-hybrid-decompose-routing-for-artifact-builds.md) | HYBRID/DECOMPOSE Routing for High-Complexity Artifact Builds | Retired 2026-07-15 (FRE-884) — superseded by ADR-0118 |
| [ADR-0087](ADR-0087-memory-recall-quality-measurement-program.md) | Memory-Recall Quality: A Measurement-First Program (Diagnose → Gate → Architecture) | Accepted — 2026-06-27 |
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
| [ADR-0098](ADR-0098-memory-substrate-and-lifecycle-architecture.md) | Memory Substrate & Lifecycle Architecture (Core/Docs topology; living-knowledge model) | Accepted (amended 2026-08-30 — Amendment A: provenance chains terminate outside the agent; the retrieval tool declares its referent; provenance as an append-only edge written atomically with the entity/relationship; entitlement follows the terminus) |
| [ADR-0099](ADR-0099-configuration-management-and-validation.md) | Configuration Management & Validation (single-source role matrix + validator) | Implemented; amended by ADR-0121 |
| [ADR-0100](ADR-0100-relevance-bounded-recall.md) | Memory Recall — Relevance-Bounded Candidate Generation | Accepted |
| [ADR-0101](ADR-0101-agent-vision-ingestion.md) | Agent Vision Ingestion of Uploaded Images | Accepted |
| [ADR-0102](ADR-0102-document-ingestion.md) | Document Ingestion (PDF) — tiered, capability-routed | Accepted |
| [ADR-0103](ADR-0103-recall-no-clean-floor-structural-separation.md) | Recall is Retrieval — No Clean Similarity Floor; Separation is Structural | Accepted |
| [ADR-0104](ADR-0104-multi-path-retrieval-rank-fusion.md) | Multi-Path Retrieval with Rank Fusion | Proposed |
| [ADR-0105](ADR-0105-convergent-self-improvement-pipeline-and-system-graph.md) | Convergent Self-Improvement Pipeline & Isolated System Graph | Accepted |
| [ADR-0106](ADR-0106-system-user-knowledge-boundary-dispatch-observe-ground.md) | The System/User Knowledge Boundary — Dispatch by Output Kind, Observe, Ground | Superseded by ADR-0115 (2026-07-11) |
| [ADR-0107](ADR-0107-user-identity-resolution-and-log-propagation.md) | User Identity Resolution for Claims + Trace/Log Identity Propagation | Accepted |
| [ADR-0108](ADR-0108-stored-artifact-vision-reprocessing.md) | Stored-Artifact Vision Re-processing (analyze-to-text, explicit tool) | Proposed |
| [ADR-0109](ADR-0109-entity-taxonomy-redesign.md) | Entity & Relationship Taxonomy — V1 (inherited) → V2 (first principled derivation) | Accepted |

### Dispatch, Delivery Process & Substrate (ADR-0110 – ADR-0137)

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
| [ADR-0121](ADR-0121-model-catalog-and-selection-layer.md) | Model Catalog and Selection Layer — Providers, Deployments, Bindings; the User Selects the Model | Implemented — 2026-07-22; Addendum A (per-primary sub_agent mapping, FRE-964) Proposed |
| [ADR-0122](ADR-0122-build-time-artifact-builder-selection.md) | Per-Build Artifact Builder Selection — Choose the Model Before the Plan Is Written | Implemented — 2026-07-22 (amended 2026-07-21 — card at turn start) |
| [ADR-0123](ADR-0123-turn-progress-surface.md) | Turn Progress Surface — Make the Wait Legible, So the User Stays Attached | Accepted — 2026-07-24 |
| [ADR-0124](ADR-0124-session-summary-producer-and-phased-consumption.md) | Session-Summary Producer Correction and Phased Consumption | Accepted (amended 2026-07-23 — Amendment A, conversation-scoped input; 2026-07-24 — Amendment B, conversation-only: `tool_evidence` + `status_contradiction` removed) |
| [ADR-0125](ADR-0125-two-quality-dimensions-and-turn-evidence-contract.md) | The Two Quality Dimensions and the Turn Evidence Contract (supersedes ADR-0067 reflection surfacing; verification oracle deferred) | Accepted |
| [ADR-0126](ADR-0126-reading-the-living-knowledge-substrate.md) | Reading the Living-Knowledge Substrate — Stance-First Push, Pull-Only Claims (supplies ADR-0098's read half) | Accepted |
| [ADR-0127](ADR-0127-harness-self-analysis-pillar.md) | The Harness Self-Analysis Pillar — Collectors Emit Facts, One Analyzer Judges, Findings Are Keyed by Evidence | Proposed |
| [ADR-0128](ADR-0128-telemetry-naming-and-structure-convention.md) | One Telemetry Naming and Structure Convention Across Every Substrate — Enforced at Emit and at the Substrate Boundary (closes ADR-0090's deferred field registry; adopts ADR-0093's OTel choice) | Superseded |
| [ADR-0129](ADR-0129-opentelemetry-instrumentation-and-trace-visibility.md) | OpenTelemetry Instrumentation, with Trace Visibility as the Acceptance Bar — SDK context propagation, Collector, Tempo (un-parks ADR-0093 D3 and supersedes FRE-588's Elastic route; supersedes ADR-0128's enforcement mechanisms) | Accepted |
| [ADR-0130](ADR-0130-two-tiers-of-acceptance-criteria.md) | Two Tiers of Acceptance Criteria — A Sub-Ticket Proves Its Own Work, One Seam Ticket Proves the ADR (severs criterion inheritance across the four contract documents) | Superseded |
| [ADR-0131](ADR-0131-retire-master-plan-owner-console.md) | Retire MASTER_PLAN — an Owner Console with an Explicit Trust Ladder, One Writer per Store | Accepted |
| [ADR-0132](ADR-0132-outbound-authenticated-egress.md) | Outbound Authenticated Egress — Caddy Terminates the Cloudflare Barrier; Application Credentials Stay in the Application | Accepted |
| [ADR-0133](ADR-0133-typed-emit-envelope-residual-log-corpus.md) | The Typed Emit Envelope for the Residual Log Corpus — Enforcement by Declared Retired Spelling, and the Field Registry Declined (restores ADR-0128 Tier 1 on the surface ADR-0129 leaves ungoverned; answers ADR-0090's deferred registry question *no*) | Proposed |
| [ADR-0134](ADR-0134-activity-alerting-absence-as-a-first-class-signal.md) | Activity Alerting — Absence as a First-Class Signal, on Platform-Native Alerting (five instruments, no egress; declines ADR-0090's proposed fourth corner and amends its D6 done-bar instead) | Proposed |
| [ADR-0135](ADR-0135-explore-seat-working-contract.md) | The Explore Seat's Working Contract — Findings Carry Evidence, Proposals Carry Feasibility, and the Gate Sits at the Exit (amends ADR-0131 D4's filing plane for `cc-explore`; re-scopes FRE-977 into a `stream:explore` dispatch) | Accepted |
| [ADR-0136](ADR-0136-cloudflare-edge-carries-http-not-grpc.md) | The Cloudflare Edge Carries HTTP, Not gRPC — the Zone gRPC Toggle Stays Off, and Protocol Conversion Happens Before the Edge (Access does not enforce on gRPC and the toggle is zone-wide; records the constraint FRE-1220 Proposal 5 asked for) | Accepted |
| [ADR-0137](ADR-0137-two-party-obligations-and-cutover-tickets.md) | A Two-Party Obligation Is Split at Authoring, and a Cutover Is Two Tickets — the Dispatch Question Is Only the Backstop (extends ADR-0130 D1's coverage clause and D6's dispatch check; preserves ADR-0033's clean-break case) | Superseded |
| [ADR-0138](ADR-0138-the-model-may-generate-but-may-not-assert.md) | The Model May Generate, But It May Not Assert — Verified Citations as Seshat's Grounding Contract, Tier-Invariant and Enforced by Measured Compliance (parametric knowledge is never a source; deletes the "Do NOT say you have no memory" prohibition and the recency-keyed search trigger) | Accepted (amended 2026-08-25 — D2 `curl` illustration corrected; amended 2026-09-01 — D2 narrowed by ADR-0098 Amendment A §A6, entitlement follows the provenance-chain terminus, implemented FRE-1347) |
| [ADR-0139](ADR-0139-what-the-agent-learns-by-doing.md) | What the Agent Learns by Doing — a Denominator for the Compliance Metric, and Attachments as First-Person Observation (absorbs FRE-1316 vision; D2, D3 and D7-except-row-one withdrawn and D6 retired under ADR-0140 — the ADR-0138 D2 amendment lapses with them) | Proposed — partially withdrawn 2026-09-02 (live: D1 as amended, D4, D5, D8) |
| [ADR-0140](ADR-0140-the-model-is-not-a-security-boundary.md) | The Model Is Not a Security Boundary — Seshat's Declared Threat Model, and the Layer Rule Every Other ADR Cites (intent is not a design input; inputs are untrusted; a model-layer control may never be the sole control for an invariant; retires ADR-0139 D6; restores ADR-0138 D2's invocation axis and narrows it once — a query-language parameter composes) | Proposed |
| [ADR-0141](ADR-0141-one-llm-dispatch-path.md) | One LLM Dispatch Path — Every Call Rides litellm, and the Wire Is Verified, Not Assumed (closes ADR-0031 Alt C; supersedes ADR-0121's vocabulary-by-dispatch-path and delivers its inert cloud ceilings; extends ADR-0132's guard to the whole path; the four inert sampler/thinking params go live with wire-level proof; dissolves FRE-1343) | Accepted 2026-09-03 |
| [ADR-0142](ADR-0142-capability-is-not-a-property-of-register.md) | Capability Is Not a Property of Register — a Turn Earns Its Budget by Demonstrating Need, and the User Arbitrates (deletes the per-TaskType iteration cap and `conversational_always_single`; binds fan-out to the brainstem budget; a spend threshold raises an ADR-0076 constraint pause instead of a kill, and that crossing is also the demonstrated-need escalation trigger; human wait stops consuming the turn deadline, bounded by a new unextendable lifetime cap) | Accepted |
| [ADR-0143](ADR-0143-the-request-task-is-the-unit-that-must-end.md) | The Request Task Is the Unit That Must End — Bound the Service Task, Not the Turn (the chat turn task is created through a service registry, carries a cooperative wall-clock bound above the orchestrator's cap, and the registry closes the client stream on a deadline through a close-once arbiter even when the task never returns — a task that outlives it is kept as leaked and counted, never silent; the orchestrator cap's description is corrected to its real bind points and gains a per-step check; measured the FRE-1403 "hang" and found a completed turn plus two adjacent defects filed separately) | Proposed |
| [ADR-0144](ADR-0144-local-call-failure-and-the-human-retry.md) | A Local Model Call Fails Honestly, and the Human Decides the Retry (corrects FRE-1398's premise — Cloudflare's 120s Proxy Read Timeout is a gap bound, not a total bound, so only the `httpx` read arm is dead, not `asyncio.timeout`; that arm becomes a measured gap bound below 120s so a silent origin raises our typed error rather than a 524 page; the retry contract is stated in origin requests and counted at the transport, because four sources disagreed on one incident's attempt count, and litellm's `2n+1` expansion cannot express two — so the local path owns a two-attempt loop outside the slot, retried only for a gap timeout or a connection failure; retry exhaustion raises an ADR-0076 `model_unreachable` pause on the local primary call only, with `allow_preference=False` so no remembered answer restores the automatic loop, and `stop_and_report` returns the completed sub-agent work; health stops being a polled GET — the failing call publishes the verdict itself because a stall ends inside the 300s probe interval, the probe covers only the idle box, and a four-rule state model orders the two writers by observation time, makes progress outrank silence across concurrent calls, and keeps a `down` sticky until positive evidence clears it) | Proposed |
| [ADR-0145](ADR-0145-two-nouns-and-the-dialect-between-them.md) | Two Nouns and the Dialect Between Them — Finish ADR-0121 (the schema shipped in July and the catalog was never migrated onto it, so every symptom the owner's "terribly difficult to manage" commission named traces to one un-migrated fact; the local thinking/instruct pairs collapse to one entry per served artifact and `inherit` resolves the sub-agent against the session's primary in all four paths, retiring `defaults_by_primary` — measured dead on a live turn — and superseding FRE-966/FRE-967; the resolver stops dropping binding overrides on a redirect and instead sorts a field by what it is, so budget always applies, mode fields resolve through the selected model, and samplers live only inside a mode and never cross a redirect — which is why `RoleBinding` loses `temperature`/`disable_thinking`/`reasoning_effort` and gains `mode`/`priority`; a dialect is declared on the provider and overridable per model because placement predicts neither the thinking lever nor the accepted sampler set — OVH is cloud-placed and refuses Qwen-native fields, and two Anthropic models disagree with each other — and that declaration, not litellm's map, becomes the reasoning oracle wherever litellm is not the wire, since `get_supported_openai_params("ovhcloud")` is wrong at the provider level and cannot become right; boot reconciliation shrinks to calling the FRE-1415 probe at startup and widening it past `id`, reporting drift as a `config_guard` finding that never blocks a boot on a sleeping laptop; a worker runs at its dialect's own cheap mode because "inherit at low effort" is wrong on three of five dialects and `low` is the cheapest setting only on Sonnet 5; and concurrency stays on the provider and the model with the role carrying an `InferencePriority`, which is a real widening — two primaries serialize today at the thinking entry's limit of 1 and will not after the collapse — stated as a prediction against the 131072 window and measured, not capped in advance) | Accepted |
| [ADR-0146](ADR-0146-the-answer-must-be-unguessable.md) | The Answer Must Be Unguessable — Proving a Model Used the Tool, and Making the Primary Role Depend On It (the ADR-0138/0139/0140 chain designed against a model that retrieves and misreports, but every observed failure is a model that does not retrieve at all — FRE-1327's invented table and the owner's local-model comparison are both that defect, and under ADR-0140 an honest uncitable report and a fabricated one score identically; so a probe is a question whose correct answer cannot be produced without invoking the tool, generalised from the owner's own sub-agent proof query, scored only on volatile live values because replica counts and versions are guessable, with a fresh unguessable value every run so the second sequential model cannot pass by recall; four verdicts including an `INVALID` cell that makes the instrument detect its own contamination; web search is excluded rather than faked; nothing is written to a substrate so FRE-375 never engages; and the part that changes an outcome — a model below the pass bar cannot hold the `primary` role, the probe recurs because tool use drifts with a serving change, and the bar is fixed before the first scored run; supersedes FRE-1361's direction and parks the citation-obligation design with its buildability finding, that an ADR-0138 exempt span is never verified and never counted so "exempt but checked" has no representation) | Proposed |
| [ADR-0147](ADR-0147-memory-belongs-to-the-planner.md) | Memory Belongs to the Planner — a Bounded Digest Shapes the Specs, and No Memory Section Reaches the Worker (the one component that decides the work was the only one denied user context: `memory_in_context` is False on all 186 sub-agent captures ever written, and the planner builds two messages carrying only a tool surface and the raw query, running at 289 input tokens against a turn that assembled 516 memory tokens; the finding that changes the cost argument is that the items are already in hand — `step_init` sets `ctx.memory_context` 143 lines before the expansion call — so nothing here issues a recall query and FRE-960's dead multi-query arm is a quality ceiling rather than a blocker; FRE-1470 bundled two designs and the owner settled it as inform-only, because the primary already holds full memory at synthesis so a worker never needs to hold a fact, only to be asked the right question — which also keeps a `web_search`-holding worker off the disclosure path that FRE-1467 widens when it lands; one clause per line rather than bare names, since a names-only list produces goals naming facts the worker cannot resolve — and codex round 1 then showed that clause makes the clean separation false, because a goal is sent to the worker verbatim, so D2 now guarantees only what it can (no memory item in `spec.context`, no rendered section to a worker) and names the exposure it does not close, an inlined fact entering a `web_search` query with nothing mechanically bounding it; the digest is defined over the primary renderer's own filter-then-cap selection through a shared function, so nothing can reach a worker that the answering model did not also receive and a Stage-7 drop yields no digest; bounded independently at 20 items, 120 characters per line and 300 estimated tokens because an unbounded one-line-per-item digest over the 47-item render bounds reaches ~700 tokens and is dearer than the section it replaces, with the block paid once per turn but a copied clause paid again per arm; the plan gains a `memory_relevance` recording the planner's judgment and nothing else, after round 2 showed a five-value enum demanding two values at once on an empty-digest fallback — availability and plan-source are two other axes, logged beside it — and `none_relevant`, scoped to the digest, is the system's first honest absence signal, leaving FRE-1118's utterance to FRE-1118; every planner attempt must emit a terminal event, since today a fallback emits none and biases any rate computed over `planner_completed`; a validation sub-agent arm and any enrichment write path are both rejected; and the criteria are the part that moved most — the live lexical check that would have proved "the planner used the digest" is cut on the ground that it is not even necessary, since a clause can shape a goal without the goal repeating a name, replaced by paired seeded planner runs where the relevant fact must change a goal and the unrelated digest must return `none_relevant`, with the invented 25% production floor dropped for a discrimination check and the reason stated: no defensible rate exists in advance while FRE-960 leaves recall one blunt query, and round 3 then closed the last gap by making the seeded value a coined token the test owns, which is why it is not the rejected lexical check under another name — the test owns the seed so absence is a real failure, where on a live turn nothing says which recalled term mattered; and the ACs carry three stated limits — they prove the mechanism rather than the answer quality, none of them bounds what a goal discloses, and a live `used` claim is never verified against its own plan, which is a consequence of rejecting structured per-task references, deliberately taken) | Proposed |
| [ADR-0148](ADR-0148-absence-must-be-reachable-and-sayable.md) | Absence Must Be Reachable and Sayable — FRE-1118 named three mechanisms and ADR-0138 shipped two of them (FRE-1287's floors, FRE-1283's `"Do NOT say you have no memory."`) without anyone closing the ticket, so what remains is not the missing score the title claims but a missing representation: `memory_context` is a bare list or `None`, `dense_recall_arm` documents its own collapse (*"Empty when disconnected, the query is empty, embedding fails, or nothing clears the floor"*), and the executor appends no section at all when the list is empty — so honest absence, a failed arm, a Stage-7 budget drop and unwired memory all reach the model as identical silence, while the one degradation flag that exists is gated to `MEMORY_RECALL` (recall itself is not intent-gated) and is consumed only by `route_trace`; the decision is four rendered states on a single axis, what the model may honestly conclude, with the section always present and `WITHHELD` kept separate from `UNAVAILABLE` because a budget drop is the one non-populated case where the system knows items existed and can say so actionably, the full cause staying in the turn-evidence record and the status defaulting to the weaker claim on the pattern `RecallDiscardReport` already sets; but the load-bearing half arrived only after the owner challenged a proposed deferral as laziness, and it is that **absence is not reachable today** for two independent reasons, the second of which needs no measurement at all: `_inject_behavioural_stances` runs on every authenticated turn, states in its own docstring that it runs "even when `memory_context` is `None`", and builds a populated list when it is — so any status derived from container emptiness reads `POPULATED` on essentially every turn, which is why D2 scopes the status to the **recall layer** rather than the container; and separately FRE-1287 removed the floors while leaving the weights, against an embedder whose measured negative *maximum* of 0.792 exceeds its positive median of 0.776, where the negative statistic is each query's **strongest non-match** — which is exactly the candidate admission is decided on, since a vector search returns its nearest neighbours and on an absent query the nearest neighbour is the strongest non-match — contributing 0.185 from embedding alone so recency carries anything younger than ~24 days over the 0.30 bar, a case FRE-1287's own test never saw because it pinned `vector_score=0.5` (orthogonal) rather than the measured 0.706; hence D4, which took three review rounds to place correctly: the obligation binds at the **admission boundary** in `context.py`, one layer above the recall core, because the core states at `service.py:5111-5120` that it "never applies a score threshold to the fused or reranked set — the reranker orders, it does not gate" and its structural arm is "a plain closed-axis (recency-ordered) scan with no caller-supplied predicate", so ADR-0103/0104 stay untouched while gating moves to the consumer — with one bound **per path**, since a reranker scale is not comparable to Neo4j embedding space (FRE-695), a hard gate ahead of `_combine_scores` for proactive, an acquired relevance value for the entity-match path which computes none at all today and is reached precisely when proactive admits nothing (which value is its ticket's call; **whether** is not), and for broad recall the reranker score that `_rerank_fused_items` currently **discards** after sorting, making that plumbing this ADR's own work rather than an assumption it makes; a path that can supply no relevance value at all — a disabled, raising or bypassed reranker — reports `UNAVAILABLE` instead of admitting on rank order, which is FRE-1170's disease caught in its own house; the states carry a total precedence order because they are not naturally disjoint, with any incomplete arm ranking `UNAVAILABLE` above everything so absence stays the hardest claim to reach, and `NOTHING_RELEVANT` licensing only "no **usable** record" since a blank-description entity (FRE-1115's 18.7%) is a name that matched with nothing to read, while real content the renderer cannot emit is `WITHHELD` rather than absence; no relevance band is added, on D4's strength alone, since codex round 1 established that `check_containment` can return `CONTAINED` on token presence without semantic support and the first draft's structural claim for it was too strong — the residual weakly-relevant token-matching citation is named rather than hidden; enforcement is the existing ADR-0138 contract and this ADR builds none, declaring plainly that it is honest and inert until FRE-1325 closes the loop nothing currently consumes; and FRE-1120's decision is merged here while its retry and evidence record split out as their own ticket, with FRE-1170/FRE-240 named as the same shape in the reranker and deliberately not decided | Proposed |
| [ADR-0149](ADR-0149-land-before-the-cut.md) | Land Before the Cut — a Sub-Agent Owes a Report on Every Path, and the Caller Cannot Hide a Failed Landing (FRE-1483's review of the primary's loop before FRE-1482 copies it: the tools-off call is the one mechanism worth copying, and in its cache-preserving form — a live probe shows dropping the tools array re-prefills the whole prefix at ~350 tok/s while keeping it with `tool_choice="none"` hits 4,187 of 4,191 tokens, so the primary's own local forced synthesis pays that miss today; the countdown misnames rounds as calls, its zero case invites a discarded generation, and neither loop reserves time to write; the worker contract is five moves — budget stated at round 1, date in the task message, a countdown every round carrying rounds, characters and seconds, a landing reserved before each round so the worker synthesizes when `remaining < mean_round + generation budget`, and forced synthesis at the cap rather than cap+1 so the sixth call writes instead of being thrown away — with every terminal path returning a declared `report_kind` (synthesized / narration / ledger) and `stop_reason`, the ledger listing the queries made and never the raw results; the caller's obligation is enforced by an ADR-0076 pause and a deterministic trailer the model cannot remove, because the instruction at `expansion_controller.py:1013` exists and was ignored — D4 amended 2026-09-11 at FRE-1484's gate on a codex finding: the pause fires on a failed landing (`ledger`, `narration`, a skipped task) and never on `success` alone, so a capped worker with a report proceeds with the trailer instead of pausing every research turn, and a headless caller's default is `answer_from_partial` with the trailer and a recorded policy rather than a report bundle, so an eval measures the answer; no limit changes, per the owner's directive that the landing ships before the runway is decided, and the captures gain the fields that decision needs; FRE-1389's "no wrap-up round" is reversed with the reason stated; worker findings remain uncitable under ADR-0138 D2 by design, and the shared-registry fix is named as a follow-on ADR) | Accepted |
| [ADR-0150](ADR-0150-the-worker-returns-data-not-prose.md) | The Worker Returns Data, Not Prose — a Typed Worker Registry, a Report Schema, and the Prompt That Was Never Configured (FRE-1491: every worker gets one 816-character system prompt and a 322-character task, `skill_index_block` measured at 0 characters, and the landing call's three prose instructions were obeyed one-of-three on both workers — the two that failed are the two a schema enforces; a survey of Anthropic, OpenAI, llama.cpp, Open Deep Research, smolagents, CrewAI, Magentic-One, ADK, Qwen-Agent, Tongyi DeepResearch and Qwen Code's built-in registry finds every tool-loop harness chose template-shaped prose, no harness carries a negative-finding field, and the constrained-decoding degradation of Tam et al. is overturned by dottxt and JSONSchemaBench once a reasoning field comes first — so D1 is `worker_report_v1`: bounded `working_notes` first, `findings[]` with `claim` / `source_url` / `date_or_period` / `why_it_matters`, a required `gaps[]`, every property required and every limit under llama.cpp's ~2,000-character grammar bound, the worst case sized under the landing ceiling that exists rather than the ceiling raised, one constrained landing call on every path including the voluntary stop (the worker replies `DONE`, then is asked for its report) because which call is the landing is known only after it returns, a validity table giving every finish reason a disposition with an empty report a ledger, a per-dialect `LANDING_ACCEPTS_JSON_SCHEMA` table on ADR-0149 D6's pattern, and a deterministic markdown rendering crossing to the primary with the gaps laid out once; D2 is a closed `WorkerType` registry with `researcher` and `general` after the owner's observation that Qwen Code's types bundle description, prompt, tools and return shape, re-scoping ADR-0149 AC-4a to identical bytes per type since the tools array is in the cached prefix anyway; D3 is a named thoroughness level per task whose round budgets ship equal to the cap so FRE-1487 has a field to fill without any limit changing, with the budget number moved from the system prompt to the task message; D4 deletes the planner-mandated combine worker, which ran sequentially with only the last four messages, re-did the research, and was the evidence run's only fatal failure; D5 adds Qwen Code's "do not guess" and "complete only the assigned task" lines, an isolation line, a sibling line, and a `researcher` block with source guidance, the not-carry-back rule and the found-nothing rule; D6 reads `finish_reason` so worker 1's 8,192-token cut landing stops being recorded `synthesized`; AC-2 measures the schema against prose on the study's own quality criterion with Option 2's two-call form as the recorded fallback) | Accepted |

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
