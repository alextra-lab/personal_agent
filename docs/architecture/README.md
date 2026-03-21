# Architecture Documentation

This directory contains detailed architecture specifications and design documents for the Personal Agent system.

## Current Architecture (Redesign v2)

The primary architecture reference is the **[Cognitive Architecture Redesign v2](../specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md)** specification. It supersedes ADR-0017 (Three-Tier Multi-Agent Orchestration) and evolves ADR-0018 (Seshat Memory Librarian).

Key concepts: Pre-LLM Gateway (7-stage deterministic pipeline), single-brain primary agent (Qwen3.5-35B), Seshat MemoryProtocol, expansion/contraction via sub-agents and external delegation, brainstem-driven homeostasis.

Implementation plans:
- [Slice 1: Foundation](../superpowers/plans/2026-03-16-slice-1-foundation.md) — Complete
- [Slice 2: Expansion](../superpowers/plans/2026-03-18-slice-2-expansion.md) — Complete
- Slice 3: Intelligence — Planned (see spec Section 8.3)

*Status: Evaluation phase — building real usage traces before Slice 3.*

## Historical Architecture Documents

The documents below describe earlier architectural designs. They are retained for historical context. See the superseded banners at the top of each document for details.

## Core Architecture Documents

- **[System Architecture](system_architecture_v0.1.md)** - High-level system design and architectural objectives
- **[Cognitive Agent Architecture](COGNITIVE_AGENT_ARCHITECTURE_v0.1.md)** - Brain-inspired cognitive architecture for the orchestrator *(historical — superseded by Redesign v2)*
- **[Orchestrator Core Spec](ORCHESTRATOR_CORE_SPEC_v0.1.md)** - Orchestrator implementation interfaces and runtime structure *(historical — superseded by Redesign v2)*
- **[Homeostasis Model](HOMEOSTASIS_MODEL.md)** - System stability and self-regulation mechanisms
- **[Human Systems Mapping](HUMAN_SYSTEMS_MAPPING.md)** - Biological inspiration for system design

## Service Specifications

- **[Brainstem Service](BRAINSTEM_SERVICE_v0.1.md)** - Homeostasis and monitoring service
- **[Local LLM Client Spec](LOCAL_LLM_CLIENT_SPEC_v0.1.md)** - LLM client interface and integration
- **[Service Implementation Spec](SERVICE_IMPLEMENTATION_SPEC_v0.1.md)** - Service architecture and implementation details

## Specialized Components

- **[Intelligent Routing Patterns](INTELLIGENT_ROUTING_PATTERNS_v0.1.md)** - Model routing and selection strategies *(historical — superseded by Redesign v2)*
- **[Router Self-Tuning Architecture](ROUTER_SELF_TUNING_ARCHITECTURE_v0.1.md)** - Adaptive routing optimization *(historical — superseded by Redesign v2)*
- **[Control Loops & Sensors](CONTROL_LOOPS_SENSORS_v0.1.md)** - Monitoring and feedback mechanisms
- **[Request Monitor Spec](REQUEST_MONITOR_SPEC_v0.1.md)** - Request monitoring and metrics
- **[Request Monitor Orchestrator Integration](REQUEST_MONITOR_ORCHESTRATOR_INTEGRATION_v0.1.md)** - Integration patterns
- **[System Health Monitoring Data Structures](SYSTEM_HEALTH_MONITORING_DATA_STRUCTURES_v0.1.md)** - Health monitoring schemas
- **[Tool Execution Validation Spec](TOOL_EXECUTION_VALIDATION_SPEC_v0.1.md)** - Tool safety and validation

## Technology & Stack

- **[Stack and Language Choices](stack_and_language_choices_v0.1.md)** - Technology decisions and rationale

## Diagrams

- **[C4 Context and Container](diagrams/c4_context_and_container.md)** - System context and container diagrams
- **[Nervous System Orchestration](diagrams/nervous_system_orchestration.md)** - Orchestration flow diagrams

## Experiments

- **[E-005: Router Parameter Passing Evaluation](experiments/E-005-router-parameter-passing-evaluation.md)**
- **[E-006: Router Output Format Detection](experiments/E-006-router-output-format-detection.md)**
- **[E-007: Thinking Router Model Optimization](experiments/E-007-thinking-router-model-optimization.md)**

## Inspiration & Research

- **[Production Grade System Inspiration](INSPIRATION_production_grade_system.md)** - Reference architectures and patterns

---

**Note**: These documents are living specifications that evolve with the system. Version numbers (v0.1) indicate the current iteration of each document.
