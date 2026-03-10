# Automatic Context Switching and Task Segmentation in Agent Systems

**Status**: Complete
**Date**: 2026-03-10
**Sources**: Perplexity deep research synthesis, academic papers, practitioner writeups (see References)
**Related**: ADR-0017 (Three-Tier Orchestration), ADR-0018 (Seshat Memory Librarian), ADR-0024 (Session Graph Model), ADR-0006 (Orchestrator Runtime)

---

## Executive Summary

This document analyses the problem of **automatic, internal task and context management** within an agent layer: detecting when a user has shifted topics or started a new task within a single conversation, routing each task to its own scoped context, and managing multiple concurrent internal tasks without exposing that complexity to the user.

Research and practice converge on a pipeline of **automatic topic/intent shift detection + event segmentation + context engineering + scheduling** on top of a shared memory layer. Our project already has strong foundations for several of these capabilities (context isolation, memory graph, tiered orchestration) but lacks the critical **boundary detection** and **task registry** layers that would enable automatic context switching.

**Recommendation**: This is a research area with concrete experimentation prerequisites. The multi-agent orchestration (ADR-0017) must be operational before meaningful experiments can run. One component — the Task object in the memory graph — may warrant an early ADR as a natural extension of ADR-0024.

---

## 1. Problem Statement

The Personal Agent currently treats each user message as belonging to one monolithic session. The orchestrator processes requests in a request-response cycle (ADR-0006: INIT -> LLM_CALL -> TOOL -> SYNTHESIS -> COMPLETED) with no mechanism to:

1. **Detect** that the user has shifted from "debug this Python error" to "what's the weather in Marseille" mid-session
2. **Segment** the conversation into discrete task objects that persist across turns
3. **Switch** the active context — selecting only the memory, history, and artifacts relevant to the current task
4. **Pause/resume** internal tasks when the user interleaves multiple lines of inquiry

In a single-user local agent, the user is currently the implicit task manager. They know when they've switched topics. However, as the agent handles longer, multi-topic sessions — or eventually serves concurrent contexts (e.g., SOC analyst workflows) — automatic task segmentation becomes critical for maintaining context quality and preventing the "context rot" that ADR-0017 identifies as the primary motivation for multi-agent architecture.

---

## 2. Core Capabilities Required

### 2.1 Automatic Task Boundary Detection

Detect when a user has started a new task or shifted topics inside the same conversation, and spin up or switch to a new internal task object.

**Approaches from the literature:**

| Approach | Mechanism | Applicability to Our System |
|---|---|---|
| **Supervised topic-break detection** | Multi-task neural models using sentence embeddings + predicted intention + speaker signals; outputs boundary/no-boundary label between utterances | Requires training data; could use our Session/Turn graph as a labeled dataset once populated |
| **Unsupervised shift detection** | Track semantic trajectory of message embeddings over time, smooth it, detect peaks where semantic direction changes sharply, cluster segments into tasks | More practical for cold-start; no labeled data needed; fits our embedding infrastructure |
| **LLM-based event segmentation** | Use the LLM itself to mark event boundaries in conversational streams, then treat boundaries as anchors for memory and recall | Natural fit — could run as a lightweight classification prompt on each user utterance |
| **Intent-based detection** | Classify each utterance's intent and detect when intent category changes (e.g., "code_debug" -> "weather_query") | Already partially implemented via our router's complexity/risk classification (ADR-0017 §2.4) |

**Key insight**: Boundary detection is **latency-sensitive** — it runs on every user utterance before routing. This constrains model selection.

### 2.2 Task Object Registry

Explicit task entities in the orchestrator, independent of UI threads:

- **Task ID**: Unique identifier
- **Title**: Auto-generated or user-corrected (e.g., "Debug Python import error", "Weather check Marseille")
- **Goal/Description**: What the user is trying to accomplish
- **Status**: active, paused, completed, abandoned
- **Responsible agent(s)**: Which worker(s) are handling subtasks
- **Pointers**: References to relevant messages (Turns), tools invoked, artifacts produced
- **Memory slice**: Which entities, sessions, and facts are relevant to this task

This is the missing abstraction layer between Session and Turn in our current architecture.

### 2.3 Task-Aware Context Selection

For each agent turn, select only the subset of history and memory relevant to the current task, instead of feeding the entire transcript:

- **Per-task message history**: Only the turns belonging to the active task
- **Per-task memory retrieval**: Seshat assembles context scoped to the task's entities and domain
- **Rolling summaries**: Compress long task histories when they exceed the context budget
- **Cross-task references**: Allow the agent to note "this relates to your earlier task X" without polluting the active context

Our architecture already supports this via context isolation (ADR-0017 §2.6) and Seshat's `assemble_context(task, agent)` (ADR-0018 §2.2). The gap is the triggering mechanism — we need boundary detection to know *when* to switch contexts.

### 2.4 Orchestrated Multi-Tasking

Allow multiple internal tasks to be active, scheduled, and paused/resumed:

- **Active task pointer**: The orchestrator tracks which task is currently being served
- **Task switching**: On boundary detection, pause the current task and either resume a previous one or create a new one
- **Background tasks**: Tasks that were paused can have background work queued (e.g., "while the user works on task B, fetch the API docs they'll need for task A")
- **Event-driven transitions**: Treat "user message", "tool result", "timer", and "topic shift detected" as events that can trigger state transitions in a per-task state machine

---

## 3. Mapping to Our Architecture

### 3.1 What We Already Have

| Capability | Our Component | Status |
|---|---|---|
| **Context isolation** | Workers receive task-scoped context only, return compressed summaries (ADR-0017 §2.6) | Designed, not yet implemented |
| **Scoped context windows** | `context_window.py` — token budgeting, truncation, reserved overhead | Implemented |
| **Memory graph** | Neo4j with Session -> Turn -> Entity schema, 6-type memory taxonomy (ADR-0024, ADR-0018) | Partially implemented |
| **Context assembly** | Seshat's `assemble_context(task, agent)` — builds relevant bundles per request | Designed, not yet implemented |
| **Modular architecture** | Router (Tier 0), Orchestrator (Tier 1), Workers (Tier 2), Memory (Seshat), Monitoring (Brainstem) | Designed, partially implemented |
| **Background scheduling** | Brainstem scheduler for consolidation, reflection, health cycles | Implemented |
| **Intent classification** | Router SLM classifies complexity (simple/moderate/complex) and risk (read-only/state-modifying) | Implemented (heuristic + router) |

### 3.2 What We're Missing

| Capability | Gap | Severity |
|---|---|---|
| **Task boundary detection** | No mechanism to detect topic shifts mid-conversation | Significant — but less critical for single-user with explicit sessions |
| **Task object registry** | No `Task` entity with id/title/goal/status; `ExecutionContext` is per-request, not per-task | Significant — the missing abstraction between Session and Turn |
| **Multi-task state machine** | Orchestrator runs request-to-completion; no ability to pause task A, handle task B, resume task A | Moderate — matters for complex multi-step investigations |
| **Per-task context switching** | No active task pointer; no mechanism to swap the context assembly scope when tasks change | Moderate — blocked by missing boundary detection |
| **User-facing task affordances** | No UI to show "I detected a new task" or let the user correct task assignments | Low — can default to automatic mode |

### 3.3 Where These Fit in the Architecture

```
                    Existing                              New (this research)
                    --------                              -------------------

User Message ──► Router SLM (Tier 0)                   + Task Boundary Detector
                 complexity × risk                        continuation vs. new-task
                        │                                         │
                        ▼                                         ▼
                 Orchestrator (Tier 1)                    + Task Registry
                 decompose, dispatch,                      create/resume/pause
                 evaluate-rework-escalate                  active task pointer
                        │                                         │
                        ▼                                         ▼
                 Worker Pool (Tier 2)                     + Task-Scoped Context
                 isolated context per subtask               assembly per task,
                        │                                   not per session
                        ▼                                         │
                 Seshat (Memory)                          + Task nodes in Neo4j
                 assemble_context(task, agent)              between Session and Turn
```

---

## 4. Model Selection for Boundary Detection

### 4.1 Constraints

Boundary detection is an **inline operation** — it runs on every user utterance before routing. This imposes strict constraints:

- **Latency budget**: < 500ms per utterance (must not noticeably delay the response)
- **Concurrency**: Must not block the inference server for other tasks
- **Input**: Single user message + recent conversation context (last N turns)
- **Output**: Binary classification (continuation / new-task) or ternary (continuation / subtask / new-task)

### 4.2 Candidate Models

| Model | Parameters | Viability | Rationale |
|---|---|---|---|
| **liquid/lfm2.5-1.2b** (current router) | 1.2B | **No** | Already stretched doing two-axis classification (complexity × risk). Topic shift detection requires understanding conversational coherence across turns — a fundamentally different cognitive task from single-utterance classification. The model lacks the capacity for cross-utterance semantic reasoning. |
| **qwen3.5-4b-instruct** (current workhorse) | 4B | **Worth testing** | Already provisioned and fast. Instruct models handle structured output well. Sweet spot if accuracy is sufficient. This is the primary hypothesis to test. |
| **qwen3.5-9b-reasoning** | 9B | **Too slow for inline** | Likely accurate but reasoning models have high latency due to thinking tokens. Reserve for offline validation/evaluation of boundary detection quality, not for inline detection. |
| **8-15B instruct** (e.g., qwen3.5-14b-instruct) | 8-15B | **Probable sweet spot if 4B fails** | Would require provisioning a new model endpoint, adding infrastructure cost. Only justified if 4B demonstrably fails the accuracy threshold. |

### 4.3 Design Consideration: Instruct vs. Reasoning Models

For boundary detection, **instruct models are strongly preferred** over reasoning models:

- The task is classification, not open-ended reasoning
- Structured output (JSON with boundary label + confidence) is the target
- Latency is critical; thinking tokens are wasted budget
- The input is well-defined: current message + last N messages + optional task context

A reasoning model's strength (deep chain-of-thought) is unnecessary and counterproductive for this use case.

---

## 5. Hypotheses for Experimental Validation

### H1: 4B Instruct Model Boundary Detection Accuracy

> **Hypothesis**: qwen3.5-4b-instruct can detect task/topic boundaries with >80% F1 on multi-topic conversation transcripts, within a latency budget of <500ms per utterance.

**Test design**:
- Construct a labeled dataset of multi-topic conversations (can be synthetic initially, then real data from our Session/Turn graph once populated)
- Each utterance pair labeled: continuation / new-task / subtask
- Run qwen3.5-4b-instruct with a structured classification prompt
- Measure F1 score and p95 latency

**If H1 succeeds**: The 4B model can serve dual duty (workhorse + boundary detector) with no new infrastructure.

**If H1 fails**: Test with an 8-15B instruct model (H1b). If that also fails, consider an embedding-based unsupervised approach that doesn't require an LLM call per utterance.

### H2: Task-Scoped Context Improves Worker Output Quality

> **Hypothesis**: Workers receiving task-scoped context (assembled by Seshat for the specific task) produce measurably higher quality output than workers receiving full-session context, as measured by orchestrator evaluation acceptance rate.

**Test design**:
- Requires ADR-0017 multi-agent orchestration to be operational
- Run the same set of multi-topic conversations through workers with:
  - (A) Full session context
  - (B) Task-scoped context (only turns and memory relevant to the current task)
- Measure: orchestrator acceptance rate, rework rate, output relevance scores

**Rationale**: This validates whether context switching actually improves output quality, not just architectural elegance. If full-session context works fine for SLM workers (because sessions are short enough), the boundary detection investment may not be justified at current scale.

### H3: Task Objects Enable New Behavioral Analysis

> **Hypothesis**: Explicit Task nodes in the Neo4j graph (between Session and Turn) enable behavioral pattern detection that Session/Turn alone cannot — specifically: task abandonment rates, task interleaving frequency, and domain-specific task duration distributions.

**Test design**:
- Extend ADR-0024's graph schema with Task nodes
- Populate from real conversation data (manual labeling initially, then automated via H1)
- Attempt to answer queries that are impossible with Session/Turn alone:
  - "What fraction of tasks does the user abandon before completion?"
  - "Does the user interleave tasks more on architecture topics vs. debugging?"
  - "What is the average task duration by domain?"

**Rationale**: If Task nodes don't enable meaningfully new insights beyond what Session/Turn provides, the added complexity isn't justified.

---

## 6. Proposed Architecture Pattern

Based on the literature and our system's constraints, the following pattern is the target architecture — to be implemented only after ADR-0017 is operational and hypotheses are validated:

### 6.1 Front-Door Session

- Single `session_id` in the UI (unchanged from current)
- Every new user message arrives with recent transcript + metadata (timestamps, speaker, channel)
- User experience is unchanged — they just keep talking

### 6.2 Boundary + Intent Layer

- Run boundary detector on each new user message (4B instruct model, per H1)
- **If continuation**: Link message to the current active task
- **If new topic/goal**: Create a new task object in the registry; auto-title it
- **If subtask**: Create a child task linked to the parent
- Allow user override: "This is still about the previous thing" or "Treat this as a new task"

### 6.3 Task Registry

- Per-user task registry with statuses: active, paused, completed, abandoned
- Each task holds pointers to its Turns, entities, tool calls, and artifacts
- Route the message + task's memory slice to the appropriate agent/pipeline
- Registry persisted in Neo4j (as Task nodes) and in-memory for active tasks

### 6.4 Context Assembly (Per-Task)

- For each agent call, assemble context from:
  - Last few turns **for this task** (not the whole session)
  - Task summary (auto-generated)
  - Key decisions and artifacts from this task
  - Relevant external memory (RAG via Seshat, scoped to task entities)
- Compress long task histories into rolling summaries when needed
- Cross-task references included only as brief pointers, not full context

### 6.5 Task Lifecycle

```
                    ┌──────────────┐
      user msg ────►│  Boundary    │
                    │  Detector    │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
         continuation   new task    subtask
              │            │            │
              ▼            ▼            ▼
         ┌────────┐  ┌─────────┐  ┌──────────┐
         │ Resume │  │ Create  │  │ Create   │
         │ active │  │ new     │  │ child    │
         │ task   │  │ task    │  │ task     │
         └───┬────┘  └────┬────┘  └────┬─────┘
             │            │            │
             └────────────┼────────────┘
                          │
                          ▼
                   ┌──────────────┐
                   │  Task        │
                   │  Registry    │
                   │  (active     │
                   │   task ptr)  │
                   └──────┬───────┘
                          │
                          ▼
                   ┌──────────────┐
                   │  Context     │
                   │  Assembly    │
                   │  (per-task)  │
                   └──────┬───────┘
                          │
                          ▼
                   ┌──────────────┐
                   │  Orchestrator│
                   │  (Tier 1)    │
                   └──────────────┘
```

---

## 7. Relationship to Existing ADRs

| ADR | Relationship | Impact |
|---|---|---|
| **ADR-0006** (Orchestrator Runtime) | ExecutionContext would need a `task_id` field linking to the task registry. The state machine may need TASK_SWITCH transitions. | Moderate extension |
| **ADR-0017** (Three-Tier Orchestration) | Context isolation and worker dispatch already support per-task scoping. The boundary detector would integrate at the Router/Orchestrator boundary. | Natural extension — no conflict |
| **ADR-0018** (Seshat Memory Librarian) | Seshat's `assemble_context(task, agent)` already accepts a task parameter. Task objects would give Seshat a richer scope for context assembly. | Direct enabler |
| **ADR-0024** (Session Graph Model) | Task nodes would sit between Session and Turn: `Session -> Task -> Turn`. Extends the graph without breaking existing schema. | Schema extension |
| **ADR-0025** (Memory Recall Intent) | Recall queries could be scoped to a task: "What have I asked about in this task?" vs. globally. | Optional enhancement |

---

## 8. Prerequisites and Sequencing

### Must Have Before Experimentation

1. **ADR-0017 implemented**: Multi-agent orchestration with context isolation — needed for H2 (task-scoped context quality comparison)
2. **Real multi-turn conversation data**: The current graph is dominated by single-turn sessions (ADR-0024 implementation notes). Need sustained multi-topic conversations to build labeled datasets for H1
3. **4B instruct model available for classification tasks**: Already provisioned (qwen3.5-4b on port 8501)

### Sequencing

1. **Phase 1 (Now)**: Document research area (this document)
2. **Phase 2 (After ADR-0017)**: Build labeled dataset from real conversations; run H1 (boundary detection accuracy)
3. **Phase 3 (If H1 succeeds)**: Implement boundary detector as extension to Router/Orchestrator pipeline; run H2 (context quality comparison)
4. **Phase 4 (If H2 succeeds)**: Implement Task registry and Task nodes in Neo4j (potential ADR-0032); run H3 (behavioral analysis value)
5. **Phase 5 (If H3 succeeds)**: Full task lifecycle management with pause/resume and UI affordances (potential ADR-0033)

### Potential Early ADR: Task Object in Memory Graph

The Task node concept could be introduced as a lightweight ADR before the full context switching system is built, because it's a natural extension of ADR-0024's Session -> Turn model. Adding a Task node between Session and Turn doesn't require boundary detection or multi-agent orchestration — it can be populated via manual labeling initially, enabling H3 experimentation independently.

---

## 9. Literature Review and Sources

### Primary Sources (from Perplexity synthesis)

1. **Multi-Agent Context Engineering** (Vellum, 2025-2026) — Building multi-agent systems with tiered context: short-term dialogue, per-task memory, long-term global knowledge. Emphasizes context isolation so each agent operates with only what it needs. Aligns directly with ADR-0017 and ADR-0018.
   - https://www.vellum.ai/blog/multi-agent-systems-building-with-context-engineering

2. **Topic Break Detection in Interview Dialogues Using Sentence Embeddings** (PMC, 2022) — Supervised multi-task neural models that take sentence embeddings plus predicted intention and speaker signals, outputting boundary/no-boundary labels. Demonstrates that boundary detection can be treated as a classification problem with moderate-size models.
   - https://pmc.ncbi.nlm.nih.gov/articles/PMC8780003/

3. **How AI Agents Handle Multi-Tasking** (Milvus, 2025) — Overview of task registries, scheduling, and concurrent execution in agent systems. Emphasizes modular architecture with separate components for NLU, planning, execution, memory, and monitoring.
   - https://milvus.io/ai-quick-reference/how-do-ai-agents-handle-multitasking

4. **Unsupervised Topic Shift Detection in Chats** (McGill DMaS Lab, 2025) — Track semantic trajectory of message embeddings over time, smooth it, detect peaks where direction changes sharply, then cluster segments. No labeled data required — practical for cold-start.
   - https://dmas.lab.mcgill.ca/fung/pub/LFMM25amlds_preprint.pdf

5. **Event Segmentation Applications in Large Language Model Enabled Agents** (Nature, 2025) — LLMs marking event boundaries in streams; treats boundaries as anchors for memory and recall. Maps naturally to "start/stop task" in agents. Most directly relevant research to our use case.
   - https://www.nature.com/articles/s44271-025-00359-7.pdf

6. **Architecting Efficient Context-Aware Multi-Agent Framework** (Google Developers Blog, 2025) — Google's "context engineering" with tiered context isolation. Explicitly emphasizes scoped context per agent and per task.
   - https://developers.googleblog.com/architecting-efficient-context-aware-multi-agent-framework-for-production/

7. **How Task Scheduling Optimizes LLM Workflows** (Latitude.so, 2025) — Task prioritization policies (FCFS, SJF) and learning-to-rank schedulers for agent systems. Relevant to multi-task orchestration.
   - https://latitude.so/blog/how-task-scheduling-optimizes-llm-workflows

8. **LLM Workflows: From Automation to AI Agents** (YouTube, 2025) — Modular architecture patterns separating NLU, planning, execution, memory, and monitoring.
   - https://www.youtube.com/watch?v=Nm_mmRTpWLg

9. **Linguistics-Based Approach to Refining Automatic Intent Detection** (ScienceDirect, 2024) — Linguistic features for intent classification refinement. Relevant to making boundary detection more robust.
   - https://www.sciencedirect.com/science/article/pii/S0020025524014075

### Additional Context

10. **Anthropic Sub-Agents** (Boris Cherny, Jan 2026) — Context isolation as primary value of sub-agents. Already incorporated into ADR-0017's design rationale.

11. **LLM Task Automation: What Actually Works After 3 Months** (Aaron Teoh, 2025) — Practitioner lessons on task automation with LLMs. Grounding for realistic expectations.
    - https://tech.aaronteoh.com/llm-task-automation-lessons/

---

## 10. Critical Assessment

### What the Literature Gets Right

- **Context isolation matters more than specialization** — confirmed by our own ADR-0017 analysis and Anthropic's sub-agent research
- **Task objects are a natural abstraction** — every framework surveyed (LangGraph, CrewAI, AutoGen) has an explicit Task concept
- **Boundary detection is a solvable classification problem** — multiple approaches exist, from supervised to unsupervised to LLM-based

### What the Literature Assumes That Doesn't Apply to Us

- **Frontier model availability**: Most research assumes GPT-4/Claude-class models are available for all tasks. Our system runs local SLMs where a 4B model is the workhorse — boundary detection accuracy may be significantly lower than reported in papers using frontier models.
- **High-throughput multi-user**: Much of the scheduling and multi-tasking literature targets systems serving many users concurrently. Our single-user system rarely has competing tasks — the scheduling layer may be premature.
- **SOC/enterprise context**: The Perplexity synthesis was oriented toward SOC analyst workflows with multiple concurrent incidents. Our use case (personal research partner) has different task dynamics — fewer concurrent tasks, longer task lifetimes, more interleaving.

### Open Questions

1. **Is the 4B model sufficient for boundary detection?** This is the critical question. If it fails, we either need a dedicated mid-size model (infrastructure cost) or a non-LLM approach (embedding-based, lower accuracy but zero inference cost).
2. **Does boundary detection actually improve output quality for SLMs?** If our sessions are short enough that full-session context fits within effective SLM context, the investment may not be justified at current scale.
3. **Where does task boundary detection sit in the pipeline?** Before the router (Tier 0)? Between router and orchestrator (Tier 0.5)? Inside the orchestrator (Tier 1)? Each has latency and architectural implications.
4. **How should abandoned tasks be handled?** If the user starts a task and never returns to it, when does it transition from "paused" to "abandoned"? Time-based? After N other tasks? This is a design choice with memory implications.

---

## 11. Conclusion

Automatic context switching and task segmentation is a **well-defined research area** with clear relevance to our architecture. The foundations are strong — context isolation (ADR-0017), memory graph (ADR-0024), and context assembly (ADR-0018) provide the substrate. The missing pieces are boundary detection and task registry, both of which are experimentally testable.

The recommended path is **hypothesis-driven**: validate that a 4B instruct model can detect boundaries (H1), that task-scoped context improves worker quality (H2), and that task objects enable new behavioral insights (H3), before committing to architectural changes.

This area is likely to become more relevant as the agent handles longer, richer conversations and as the multi-agent orchestration matures. Documenting it now ensures the research direction is captured and the experimental framework is ready when prerequisites are met.

---

**Last Updated**: 2026-03-10
**Next Action**: After ADR-0017 multi-agent orchestration is implemented, build labeled dataset and run H1 experiment.
