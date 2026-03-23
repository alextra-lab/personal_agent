# Evaluation Dataset — 25 Conversation Paths

**Date:** 2026-03-23
**Phase:** Slices 1 & 2 Evaluation
**Companion:** `docs/guides/EVALUATION_PHASE_GUIDE.md`
**Purpose:** Structured, repeatable evaluation of every major capability in the redesigned backend

---

## How to Use This Dataset

### What This Is

25 multi-turn conversation paths organized by system capability (Capability Matrix). Each path exercises a specific subsystem and includes:

- **Turn table:** Exact user messages to send, plus expected agent behavior
- **Telemetry assertions:** Machine-verifiable via Elasticsearch or structured logs
- **Quality criteria:** Human evaluation checkboxes for response quality

### Running a Path

1. Start the agent: `uv run uvicorn personal_agent.service.app:app --reload --port 9000`
2. Open a new session (each path uses its own session for isolation)
3. Send each turn's user message in sequence, waiting for the response
4. After each turn, check telemetry assertions in Kibana (`agent-logs-*` index)
5. After the full path, evaluate quality criteria

### Scoring

For each path, record:

| Metric | Scale | Description |
|--------|-------|-------------|
| **Intent Correct** | Pass/Fail | Did `task_type` match the expected value? |
| **Strategy Correct** | Pass/Fail | Did `strategy` match the expected value? |
| **Tools Correct** | Pass/Fail | Were the expected tools called (or not called)? |
| **Response Quality** | 1-5 | Human judgment of response helpfulness and accuracy |
| **Notes** | Free text | Observations, surprises, misclassifications |

Aggregate scores feed into `docs/research/EVALUATION_PHASE_FINDINGS.md` (Week 3 deliverable).

### Prerequisites

- Infrastructure running (`./scripts/init-services.sh`)
- SLM Server running on port 8000
- Agent service running on port 9000
- Kibana accessible at `localhost:5601` with `agent-logs-*` index
- Neo4j accessible at `localhost:7474` (for memory paths)
- No pre-existing Neo4j data required — all memory paths are self-contained

---

## Capability Coverage Map

| Category | Paths | IDs | What's Verified |
|----------|-------|-----|-----------------|
| Intent Classification | 7 | CP-01 to CP-07 | One path per TaskType; correct classification and confidence |
| Decomposition Strategies | 4 | CP-08 to CP-11 | SINGLE, HYBRID, DECOMPOSE triggered by complexity; escalation across turns |
| Memory System | 4 | CP-12 to CP-15 | Entity seeding, targeted recall, broad recall, memory-informed responses |
| Expansion & Sub-Agents | 3 | CP-16 to CP-18 | HYBRID synthesis, sub-agent concurrency, budget enforcement |
| Context Management | 2 | CP-19 to CP-20 | Long conversation trimming, progressive budget management |
| Tools & Self-Inspection | 3 | CP-21 to CP-23 | system_metrics_snapshot, self_telemetry_query, search_memory |
| Edge Cases | 2 | CP-24 to CP-25 | Ambiguous intent, intent shift mid-conversation |

---

## Category 1: Intent Classification (CP-01 to CP-07)

Each path verifies that the gateway classifies one specific `TaskType` correctly. Messages are crafted to trigger exactly one regex pattern from `request_gateway/intent.py`.

---

### CP-01: Conversational Intent

**Category:** Intent Classification | **Targets:** TaskType.CONVERSATIONAL, default classification
**Objective:** Verify that simple conversational messages fall through all pattern banks to the default

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "Hey, how's it going?" | Responds conversationally. No tool calls. No sub-agents. |
| 2 | "Tell me something interesting you've learned recently." | Continues conversational tone. May draw on general knowledge. No tool calls unless proactively sharing telemetry. |

**Telemetry Assertions:**
- `task_type == "conversational"` (signal: `no_special_patterns`)
- `confidence == 0.7`
- `complexity == "simple"` (< 15 words, 0 questions, ≤ 1 action verb)
- `strategy == "single"` (reason: `conversational_always_single`)
- No `tool_call_completed` events
- No `hybrid_expansion_start` events

**Quality Criteria:**
- [ ] Response is natural and engaging, not robotic
- [ ] Appropriate length (not a one-word answer, not an essay)
- [ ] No unnecessary tool invocations or system introspection
- [ ] Turn 2 response demonstrates personality or knowledge

---

### CP-02: Memory Recall Intent

**Category:** Intent Classification | **Targets:** TaskType.MEMORY_RECALL, broad recall path
**Objective:** Verify that "have we discussed" triggers MEMORY_RECALL classification and broad recall

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "I've been thinking about building a recommendation engine using collaborative filtering." | Responds to the topic. Entities like "recommendation engine" and "collaborative filtering" should be captured for later recall. |
| 2 | "What have we discussed in our conversations so far?" | Triggers memory recall. Should reference the recommendation engine topic from Turn 1. If no prior conversation history exists, should acknowledge that and mention the current session's topics. |

**Telemetry Assertions:**
- Turn 1: `task_type == "conversational"`, `strategy == "single"`
- Turn 2: `task_type == "memory_recall"` (signal: `memory_recall_pattern`, matched by "have we discussed")
- Turn 2: `confidence == 0.9`
- Turn 2: `strategy == "single"` (reason: `memory_recall_always_single`)
- Turn 2: `memory_query_completed` event present (broad recall path if no specific entities)
- Turn 2: `search_memory_tool_called` if agent uses the tool, OR context assembly injects session history

**Quality Criteria:**
- [ ] Turn 2 response references the recommendation engine topic from Turn 1
- [ ] If no prior history, response gracefully acknowledges limited history
- [ ] Response is structured (not a wall of text) — lists topics or themes
- [ ] Does not hallucinate conversations that never happened

---

### CP-03: Analysis Intent

**Category:** Intent Classification | **Targets:** TaskType.ANALYSIS
**Objective:** Verify that "Analyze" triggers ANALYSIS classification

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "Analyze the trade-offs between REST and GraphQL for a small team building internal APIs." | Provides structured analysis comparing REST vs GraphQL. Addresses team size constraint. |
| 2 | "Which would you lean toward for our case and why?" | Provides a recommendation grounded in the prior analysis. Should reference trade-offs already discussed. |

**Telemetry Assertions:**
- Turn 1: `task_type == "analysis"` (signal: `analysis_pattern`, matched by "Analyze ")
- Turn 1: `confidence == 0.8`
- Turn 1: `complexity == "simple"` (16 words, 0 questions, 1 action verb "analyze")
- Turn 1: `strategy == "single"` (reason: `analysis_simple`)
- Turn 2: `task_type == "conversational"` (no analysis pattern in follow-up)

**Quality Criteria:**
- [ ] Turn 1 covers at least 3 trade-off dimensions (e.g., flexibility, learning curve, tooling, performance)
- [ ] Addresses the "small team" constraint specifically
- [ ] Turn 2 recommendation is consistent with Turn 1 analysis
- [ ] Structured format (bullets, headers, or numbered points)

---

### CP-04: Planning Intent

**Category:** Intent Classification | **Targets:** TaskType.PLANNING
**Objective:** Verify that "Plan" triggers PLANNING classification

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "Plan the next steps for adding user authentication to our API service." | Produces a structured plan with discrete steps, rough ordering, and considerations. |
| 2 | "What should we tackle first, and what can we defer?" | Prioritizes the steps from the plan. Should reference the plan from Turn 1. |

**Telemetry Assertions:**
- Turn 1: `task_type == "planning"` (signal: `planning_pattern`, matched by "Plan ")
- Turn 1: `confidence == 0.8`
- Turn 1: `complexity == "simple"` (13 words, 0 questions, 1 action verb)
- Turn 1: `strategy == "single"` (reason: `planning_simple`)

**Quality Criteria:**
- [ ] Plan includes at least 4 concrete steps
- [ ] Steps have a logical ordering
- [ ] Addresses auth method choices (OAuth, JWT, session-based)
- [ ] Turn 2 provides clear prioritization with reasoning

---

### CP-05: Delegation Intent

**Category:** Intent Classification | **Targets:** TaskType.DELEGATION via coding patterns
**Objective:** Verify that "write a function" keyword triggers DELEGATION classification

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "Write a function that validates email addresses and returns structured error messages for each validation rule that fails." | Classifies as DELEGATION. Composes a DelegationPackage (or produces code directly if delegation is not routed externally). |
| 2 | "Can you also add unit tests for the edge cases?" | Follow-up delegation. Should reference the function from Turn 1. |

**Telemetry Assertions:**
- Turn 1: `task_type == "delegation"` (signal: `coding_pattern`, matched by "write a function" keyword)
- Turn 1: `confidence == 0.85`
- Turn 1: `strategy == "delegate"` (reason: `delegation_route_external`)
- Turn 1: `delegation_package_composed` event (if delegation pipeline is active)
- Turn 2: `task_type == "delegation"` (signal: `coding_pattern`, matched by "unit test" keyword)

**Quality Criteria:**
- [ ] If producing code: function has type hints, docstring, and handles edge cases
- [ ] If composing DelegationPackage: task description is clear enough for an agent with no prior context
- [ ] If composing DelegationPackage: acceptance criteria are explicit and testable
- [ ] If composing DelegationPackage: context field includes project constraints and relevant files
- [ ] If composing DelegationPackage: assess whether the package is sufficient for Claude Code to act on without follow-up questions (the guide's key question: "is the context sufficient? What's missing?")
- [ ] Turn 2 response references the specific function from Turn 1
- [ ] Test cases cover meaningful edge cases (empty string, special characters, unicode)

---

### CP-06: Self-Improvement Intent

**Category:** Intent Classification | **Targets:** TaskType.SELF_IMPROVE
**Objective:** Verify that self-referential improvement questions trigger SELF_IMPROVE classification

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "What improvements would you suggest to your own memory and recall system?" | Discusses potential improvements to its own architecture. May reference Seshat, memory promotion, or recall scoring. |
| 2 | "Which of those would have the biggest impact on your usefulness to me?" | Prioritizes suggestions with reasoning grounded in actual system capabilities. |

**Telemetry Assertions:**
- Turn 1: `task_type == "self_improve"` (signal: `self_improve_pattern`, matched by "improvements...your own...memory")
- Turn 1: `confidence == 0.85`
- Turn 1: `strategy == "single"` (reason: `self_improve_always_single`)

**Quality Criteria:**
- [ ] Suggestions reference actual system capabilities (memory recall, entity extraction, promotion)
- [ ] Does not hallucinate features the system doesn't have
- [ ] Turn 2 prioritization is grounded, not generic
- [ ] Demonstrates self-awareness about current limitations

---

### CP-07: Tool Use Intent

**Category:** Intent Classification | **Targets:** TaskType.TOOL_USE
**Objective:** Verify that explicit tool-use language triggers TOOL_USE classification

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "List the tools you currently have access to." | Enumerates available tools (search_memory, system_metrics_snapshot, self_telemetry_query, read_file, list_directory, plus any MCP tools). |
| 2 | "Run the system health check and tell me if anything looks concerning." | Calls system_metrics_snapshot and/or self_telemetry_query. Reports findings. |

**Telemetry Assertions:**
- Turn 1: `task_type == "tool_use"` (signal: `tool_intent_pattern`, matched by "List...tools")
- Turn 1: `confidence == 0.8`
- Turn 1: `complexity == "simple"` (hardcoded for TOOL_USE)
- Turn 1: `strategy == "single"` (reason: `tool_use_single`)
- Turn 2: `task_type == "tool_use"` (signal: `tool_intent_pattern`, matched by "Run the...check")
- Turn 2: `tool_call_completed` event for system_metrics_snapshot or self_telemetry_query

**Quality Criteria:**
- [ ] Turn 1 lists tools accurately (matches tools registered in `tools/registry.py`)
- [ ] Turn 2 actually calls a tool (not just describes what it would do)
- [ ] Tool results are interpreted and summarized, not dumped raw
- [ ] If system is healthy, says so; if issues found, highlights them

---

## Category 2: Decomposition Strategies (CP-08 to CP-11)

These paths test that the decomposition matrix (`request_gateway/decomposition.py`) selects the correct strategy based on task type + complexity. The intent type is secondary; what matters is the strategy.

---

### CP-08: SINGLE Strategy (Simple Question)

**Category:** Decomposition | **Targets:** DecompositionStrategy.SINGLE for simple queries
**Objective:** Verify that a simple, short question results in SINGLE strategy with no expansion

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "What is dependency injection?" | Provides a clear, concise explanation. No sub-agents spawned. Single LLM call. |
| 2 | "Can you give me a quick example in Python?" | Another simple response. Still SINGLE strategy. |

**Telemetry Assertions:**
- Turn 1: `task_type == "conversational"` (no special patterns match)
- Turn 1: `complexity == "simple"` (5 words, 1 question, 0 action verbs)
- Turn 1: `strategy == "single"` (reason: `conversational_always_single`)
- No `hybrid_expansion_start` events
- No `sub_agent_spawned` events
- Single `model_call_completed` event per turn

**Quality Criteria:**
- [ ] Explanation is clear and accurate
- [ ] Appropriate depth for a definitional question (not a 500-word essay)
- [ ] Python example in Turn 2 is correct and illustrative
- [ ] Fast response time (no unnecessary overhead from expansion)

---

### CP-09: HYBRID Strategy (Moderate Analysis)

**Category:** Decomposition | **Targets:** DecompositionStrategy.HYBRID for moderate-complexity analysis
**Objective:** Verify that a moderate-complexity analysis triggers HYBRID with sub-agent expansion

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "Research the advantages of event sourcing versus CRUD for session storage, and evaluate their suitability for a PostgreSQL-backed system." | Triggers HYBRID expansion. Primary agent coordinates; sub-agents research individual aspects. Final response synthesizes sub-agent findings. |
| 2 | "Given what you found, which approach would you recommend for our use case?" | Single follow-up. Should reference the synthesized analysis from Turn 1. |

**Telemetry Assertions:**
- Turn 1: `task_type == "analysis"` (signal: `analysis_pattern`, matched by "Research ")
- Turn 1: `complexity == "moderate"` (2 action verbs: "research", "evaluate"; ≥ 2 action verbs triggers MODERATE)
- Turn 1: `strategy == "hybrid"` (reason: `analysis_moderate_hybrid`)
- Turn 1: `hybrid_expansion_start` event present
- Turn 1: `hybrid_expansion_complete` event with `successes >= 1`
- Turn 1: `sub_agent_count >= 1`
- Turn 2: `strategy == "single"` (simple follow-up)

**Quality Criteria:**
- [ ] Response covers both event sourcing AND CRUD approaches
- [ ] PostgreSQL-specific considerations addressed (JSONB for event store, ACID guarantees)
- [ ] Sub-agent contributions are synthesized coherently (not just concatenated)
- [ ] Turn 2 recommendation is grounded in Turn 1 analysis
- [ ] Quality is noticeably better than what a single-pass response would produce

---

### CP-10: DECOMPOSE Strategy (Complex Multi-Part Analysis)

**Category:** Decomposition | **Targets:** DecompositionStrategy.DECOMPOSE for complex tasks
**Objective:** Verify that a complex multi-part request with 3+ action verbs triggers DECOMPOSE

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "Compare three approaches to distributed caching, evaluate their performance under load, analyze the cost implications for each, and recommend which fits a system handling ten thousand requests per second." | Full decomposition. Multiple sub-agents research different aspects. Comprehensive synthesized output. |

**Telemetry Assertions:**
- Turn 1: `task_type == "analysis"` (signal: `analysis_pattern`, matched by "Compare ")
- Turn 1: `complexity == "complex"` (4 action verbs: "compare", "evaluate", "analyze", "recommend"; ≥ 3 action verbs + ANALYSIS = COMPLEX)
- Turn 1: `strategy == "decompose"` (reason: `analysis_complex_decompose`)
- Turn 1: `hybrid_expansion_start` event with higher `sub_agent_count` than CP-09
- Turn 1: `sub_agent_count >= 2`
- Response addresses all four requested aspects (compare, evaluate, analyze, recommend)

**Quality Criteria:**
- [ ] At least 3 caching approaches compared (e.g., Redis, Memcached, Hazelcast/Varnish)
- [ ] Performance evaluation includes metrics or benchmarks
- [ ] Cost analysis is concrete, not vague
- [ ] Recommendation is specific with clear reasoning
- [ ] Response is well-structured with clear sections for each part of the request

---

### CP-11: Complexity Escalation Across Turns

**Category:** Decomposition | **Targets:** Strategy shift from SINGLE to HYBRID across turns
**Objective:** Verify that each turn is classified independently — a simple first question doesn't lock the strategy for later complex questions

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "What is a knowledge graph?" | Simple definitional answer. SINGLE strategy. |
| 2 | "Compare Neo4j and Dgraph for entity storage, and evaluate their query performance and Python ecosystem support." | Moderate analysis. HYBRID strategy. Sub-agents spawned for this turn specifically. |
| 3 | "Based on that comparison, which should we use?" | Simple follow-up. Back to SINGLE strategy. |

**Telemetry Assertions:**
- Turn 1: `complexity == "simple"`, `strategy == "single"`
- Turn 2: `task_type == "analysis"` (matched by "Compare ")
- Turn 2: `complexity == "moderate"` (2 action verbs: "compare", "evaluate")
- Turn 2: `strategy == "hybrid"` (reason: `analysis_moderate_hybrid`)
- Turn 2: `hybrid_expansion_start` event present
- Turn 3: `strategy == "single"` (simple follow-up, no analysis patterns)

**Quality Criteria:**
- [ ] Turn 1 is concise and accurate
- [ ] Turn 2 is noticeably more detailed than Turn 1 (HYBRID effect)
- [ ] Turn 2 covers both databases across both dimensions (query performance + Python ecosystem)
- [ ] Turn 3 recommendation references Turn 2 analysis
- [ ] No "bleed-over" — Turn 1's simplicity doesn't affect Turn 2's expansion, and vice versa

---

## Category 3: Memory System (CP-12 to CP-15)

These paths test the Seshat memory system: entity extraction, storage, recall (targeted and broad), and memory-informed responses. All paths are self-contained — they seed their own entities through earlier turns.

**Important:** Memory persistence depends on the consolidation pipeline. Entity recall in Turn N may depend on whether consolidation has processed Turns 1..N-1. For same-session recall, the agent primarily uses session context (working memory), not the knowledge graph. Cross-session recall requires consolidation to have run.

---

### CP-12: Entity Seeding and Targeted Recall

**Category:** Memory | **Targets:** Entity extraction, storage, targeted recall via MEMORY_RECALL
**Objective:** Verify that entities mentioned in conversation are captured and can be recalled

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "I've been working on a project called Project Atlas. It's a data pipeline that processes satellite imagery using Apache Kafka and Apache Spark." | Responds to the topic. Entities "Project Atlas", "Apache Kafka", "Apache Spark" should be captured. |
| 2 | "The team lead is Maria Chen and we're deploying to AWS with a target of processing 500 images per hour." | Additional context. More entities: "Maria Chen", "AWS". |
| 3 | "What do you know about Project Atlas?" | Triggers MEMORY_RECALL. Should reference the data pipeline, Kafka, Spark, Maria Chen, and AWS from Turns 1-2. |

**Telemetry Assertions:**
- Turns 1-2: `task_type == "conversational"`, `strategy == "single"`
- Turn 3: `task_type == "memory_recall"` (signal: `memory_recall_pattern`, matched by "What do you know")
- Turn 3: `confidence == 0.9`
- Turn 3: `strategy == "single"` (reason: `memory_recall_always_single`)
- Turn 3: `memory_query_completed` event (if memory service queries knowledge graph)
- Session context should include Turns 1-2 content in context assembly

**Quality Criteria:**
- [ ] Turn 3 references Project Atlas by name
- [ ] Mentions at least 3 of: data pipeline, satellite imagery, Kafka, Spark, Maria Chen, AWS
- [ ] Information is accurate (not hallucinated or mixed with unrelated data)
- [ ] Response is structured — not just parroting back the user's words
- [ ] Demonstrates synthesis, not just retrieval (e.g., "Project Atlas is a data pipeline led by Maria Chen...")

---

### CP-13: Broad Recall

**Category:** Memory | **Targets:** Broad recall path ("what topics have we covered?")
**Objective:** Verify that open-ended recall questions trigger the broad recall path and return grouped results

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "I've been evaluating Django and FastAPI for our new web service. FastAPI seems faster but Django has more batteries included." | Responds to the framework comparison. |
| 2 | "We also need to decide between PostgreSQL and MongoDB for the storage layer. Our data is mostly relational but we have some document-like structures." | Responds to the database discussion. |
| 3 | "What topics have we covered in this conversation?" | Triggers MEMORY_RECALL with broad recall. Should list both the framework discussion and the database discussion as distinct topics. |

**Telemetry Assertions:**
- Turns 1-2: `task_type == "conversational"` (neither triggers analysis — "evaluating" doesn't match "evaluate\s+")
- Turn 3: `task_type == "memory_recall"` (signal: `memory_recall_pattern`, matched by "topics have we")
- Turn 3: `strategy == "single"` (reason: `memory_recall_always_single`)
- If search_memory called: `query_path == "broad_recall"` (broad keyword "topics" detected)

**Quality Criteria:**
- [ ] Response identifies at least 2 distinct topics (web frameworks, database selection)
- [ ] Mentions specific technologies discussed (Django, FastAPI, PostgreSQL, MongoDB)
- [ ] Response is organized — groups topics rather than a raw list
- [ ] Captures the user's key considerations (speed vs batteries, relational vs document)
- [ ] Does not hallucinate topics that were not discussed

---

### CP-14: Multi-Entity Tracking

**Category:** Memory | **Targets:** Correct entity association — recall the right entity, not a similar one
**Objective:** Verify that when multiple entities are introduced, the agent recalls the correct one when asked

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "Alice on our team is building a CI/CD automation tool called BuildBot. She's using Python and GitHub Actions." | Responds about Alice and BuildBot. |
| 2 | "Bob is working on a deployment tool called DeployTool. He's focused on Terraform and AWS infrastructure." | Responds about Bob and DeployTool. |
| 3 | "What do you recall about Alice and her work?" | Should recall Alice + BuildBot + Python + GitHub Actions. Should NOT conflate with Bob's work (Terraform, AWS, DeployTool). |

**Telemetry Assertions:**
- Turns 1-2: `task_type == "conversational"`, `strategy == "single"`
- Turn 3: `task_type == "memory_recall"` (signal: `memory_recall_pattern`, matched by "What do you recall")
- Turn 3: `confidence == 0.9`

**Quality Criteria:**
- [ ] Turn 3 correctly associates Alice with BuildBot, Python, GitHub Actions
- [ ] Does NOT mention Bob, DeployTool, Terraform, or AWS in the Alice recall
- [ ] Demonstrates entity-relationship awareness (Alice → builds → BuildBot)
- [ ] Clean separation between the two people and their respective tools

---

### CP-15: Memory-Informed Response

**Category:** Memory | **Targets:** Earlier conversation context shapes later responses
**Objective:** Verify that the agent uses previously established context to inform new responses, not just generic knowledge

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "I'm building a real-time dashboard using WebSockets and React for monitoring IoT sensor data from industrial equipment." | Acknowledges the project details. |
| 2 | "What technology stack would you recommend for the backend of this project?" | Should recommend technologies compatible with WebSockets, IoT data, and real-time requirements — NOT a generic "use Django" answer. Should reference the specific context (WebSockets, IoT sensors, industrial equipment). |

**Telemetry Assertions:**
- Turn 1: `task_type == "conversational"`, `strategy == "single"`
- Turn 2: `task_type == "analysis"` (signal: `analysis_pattern`, matched by "recommend...for")
- Turn 2: `complexity == "simple"` (< 15 words, 1 action verb)
- Turn 2: `strategy == "single"` (reason: `analysis_simple`)
- Context assembly for Turn 2 should include Turn 1 content

**Quality Criteria:**
- [ ] Recommendation explicitly references WebSockets established in Turn 1
- [ ] Recommendation addresses IoT/real-time requirements (not generic web stack)
- [ ] Technologies recommended are compatible with the stated stack (e.g., FastAPI/Starlette for WebSocket support, time-series DB for sensor data)
- [ ] Does not recommend technologies that conflict with stated choices (e.g., polling-based instead of WebSocket)
- [ ] Demonstrates continuity — feels like a conversation, not two isolated questions

---

## Category 4: Expansion & Sub-Agents (CP-16 to CP-18)

These paths test the HYBRID expansion pipeline: sub-agent spawning, concurrent execution, result synthesis, and budget enforcement.

---

### CP-16: HYBRID Synthesis Quality

**Category:** Expansion | **Targets:** Sub-agent result synthesis into coherent response
**Objective:** Verify that HYBRID expansion produces a synthesized response that's better than a single-pass answer

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "Research microservices communication patterns and evaluate the trade-offs between synchronous HTTP, asynchronous messaging, and gRPC." | HYBRID expansion triggered. Sub-agents research different patterns. Primary agent synthesizes into a coherent comparison. |
| 2 | "Which pattern would you recommend for a system with both low-latency and high-throughput requirements?" | Follow-up referencing Turn 1 analysis. SINGLE strategy. |

**Telemetry Assertions:**
- Turn 1: `task_type == "analysis"` (matched by "Research ")
- Turn 1: `complexity == "moderate"` (2 action verbs: "research", "evaluate")
- Turn 1: `strategy == "hybrid"` (reason: `analysis_moderate_hybrid`)
- Turn 1: `hybrid_expansion_start` event
- Turn 1: `hybrid_expansion_complete` with `successes >= 1`
- Turn 1: `sub_agent_count >= 1`
- Sub-agent `SubAgentResult` entries in logs with `summary` fields

**Quality Criteria:**
- [ ] All three communication patterns covered (HTTP, async messaging, gRPC)
- [ ] Trade-offs are concrete (latency numbers, complexity, tooling maturity)
- [ ] Response feels unified — not like three separate answers stitched together
- [ ] Synthesis adds value (e.g., comparison table, decision framework)
- [ ] Turn 2 recommendation is grounded in Turn 1 analysis
- [ ] Response quality is noticeably better than a quick single-pass answer

---

### CP-17: Sub-Agent Concurrency

**Category:** Expansion | **Targets:** Multiple sub-agents executing concurrently
**Objective:** Verify that DECOMPOSE strategy spawns multiple sub-agents and synthesizes all results

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "Compare the performance characteristics of Redis, Memcached, and Hazelcast for distributed caching. Analyze their memory management approaches and evaluate operational complexity. Recommend which fits our workload of ten thousand requests per second." | DECOMPOSE triggered. Multiple sub-agents handle different aspects. All results synthesized. |

**Telemetry Assertions:**
- Turn 1: `task_type == "analysis"` (matched by "Compare ")
- Turn 1: `complexity == "complex"` (4 action verbs: "compare", "analyze", "evaluate", "recommend")
- Turn 1: `strategy == "decompose"` (reason: `analysis_complex_decompose`)
- Turn 1: `hybrid_expansion_start` with `sub_agent_count >= 2`
- Turn 1: `max_concurrent` value in expansion logs (should respect `expansion_budget`)
- Turn 1: Multiple `SubAgentResult` entries, each with `success == true`
- Turn 1: `hybrid_expansion_complete` with `successes` matching `sub_agent_count`

**Quality Criteria:**
- [ ] All three caching systems compared (Redis, Memcached, Hazelcast)
- [ ] Performance characteristics include throughput, latency, memory efficiency
- [ ] Memory management differences explained (Redis persistence, Memcached slab allocation)
- [ ] Operational complexity addressed (setup, monitoring, cluster management)
- [ ] Final recommendation is specific and justified
- [ ] Each sub-agent's contribution is visible in the synthesized output

---

### CP-18: Expansion Budget Enforcement

**Category:** Expansion | **Targets:** `expansion_budget` limiting sub-agent spawning under resource pressure
**Objective:** Verify that when the system is under load, the expansion budget forces SINGLE even for normally-HYBRID queries

**Setup:** This path requires system resource pressure. Before running:
1. Run a CPU-intensive process to push CPU above 70% (e.g., `stress --cpu 4 --timeout 60s`)
2. OR artificially set `expansion_budget = 0` in the governance context for testing
3. Monitor `expansion_budget_computed` events in telemetry

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "Research the advantages of container orchestration and evaluate Kubernetes versus Docker Swarm for small engineering teams." | Normally this would trigger HYBRID (2 action verbs, ANALYSIS). Under resource pressure, should be forced to SINGLE. Response quality may be slightly lower without sub-agents. |

**Telemetry Assertions:**
- Turn 1: `task_type == "analysis"` (matched by "Research ")
- Turn 1: `complexity == "moderate"` (2 action verbs: "research", "evaluate")
- **Under load:** `strategy == "single"` (reason: `zero_budget` or `expansion_denied`)
- **Under load:** `decomposition_forced_single` log event
- **Under load:** No `hybrid_expansion_start` event
- **Normal conditions (control):** `strategy == "hybrid"` — run this same message without load as a comparison

**Quality Criteria:**
- [ ] Under load: Agent still provides a reasonable response (graceful degradation)
- [ ] Under load: Response is less detailed than the HYBRID version (expected trade-off)
- [ ] Budget enforcement is transparent in telemetry (reason logged)
- [ ] Compare response quality: SINGLE version vs HYBRID version of same question

---

## Category 5: Context Management (CP-19 to CP-20)

These paths test context window management: trimming long conversations and progressive budget management.

---

### CP-19: Long Conversation Trimming

**Category:** Context Management | **Targets:** Context window trimming for long conversations
**Objective:** Verify that long conversations are trimmed intelligently — important context preserved, old tool errors evicted first

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "Let's talk about our system architecture. We use a microservices pattern with FastAPI services communicating over HTTP." | Establishes foundational context. |
| 2 | "Our primary database is PostgreSQL for transactional data." | Adds more context. |
| 3 | "We also use Elasticsearch for logging and Neo4j for our knowledge graph." | More context. |
| 4 | "The deployment is on Docker Compose locally and Kubernetes in production." | More context. |
| 5 | "We've been having issues with service discovery between containers." | Introduces a problem. |
| 6 | "I tried using Consul but it added too much operational overhead." | Adds history. |
| 7 | "We're now evaluating DNS-based service discovery versus Envoy sidecar proxies." | Current state. |
| 8 | "The team is leaning toward Envoy because it also handles load balancing." | Team preference. |
| 9 | "But I'm worried about the memory overhead of running Envoy sidecars on every service." | Concern. |
| 10 | "Going back to the beginning — what was our primary database again?" | Tests whether Turn 2's content is still accessible despite context trimming. |

**Telemetry Assertions:**
- Early turns: No `context_window_applied` events (context fits in window)
- Later turns (7-10): Watch for `context_window_applied` with `truncated` details
- If trimming occurs: `input_messages > output_messages` in context window logs
- Turn 10: Agent should still know the answer (PostgreSQL) either from retained context or from understanding the conversation flow

**Quality Criteria:**
- [ ] Turn 10: Agent correctly identifies PostgreSQL as the primary database
- [ ] If context was trimmed, important foundational facts were retained
- [ ] Conversation feels coherent throughout — agent doesn't "forget" mid-conversation
- [ ] Trimming (if any) prioritizes removing less important turns, not early foundational ones
- [ ] Agent's responses in later turns remain relevant and contextual

---

### CP-20: Progressive Token Budget Management

**Category:** Context Management | **Targets:** Token budget across turns with tool outputs
**Objective:** Verify that tool-heavy conversations manage token budgets correctly

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "Run the system health check." | Calls self_telemetry_query(health). Large tool output added to context. |
| 2 | "Now show me the recent error details." | Calls self_telemetry_query(errors). More tool output added. |
| 3 | "Also check the system metrics." | Calls system_metrics_snapshot. Even more tool output. |
| 4 | "Summarize everything you've found — is the system healthy overall?" | Should synthesize all three tool results. Context may need trimming. |

**Telemetry Assertions:**
- Turns 1-3: `tool_call_completed` events for respective tools
- Turn 4: Check `context_window_applied` — old tool error messages should be evicted first
- Token estimates should increase with each turn
- If budget threshold crossed: context trimming applies, keeping recent tool results

**Quality Criteria:**
- [ ] Each tool call returns valid data
- [ ] Turn 4 synthesizes findings from all three sources
- [ ] Synthesis is coherent — not just listing each tool's output
- [ ] If context was trimmed, the most recent tool results are preserved
- [ ] Agent identifies any genuine issues from the combined health data

---

## Category 6: Tools & Self-Inspection (CP-21 to CP-23)

These paths test that the agent correctly uses its native tools, even when the intent classification is not TOOL_USE. The agent has tools available regardless of classification — the LLM decides when to use them.

---

### CP-21: System Metrics (Natural Language)

**Category:** Tools | **Targets:** system_metrics_snapshot tool usage via natural language
**Objective:** Verify that the agent calls system_metrics_snapshot even when the intent classification is CONVERSATIONAL (natural language doesn't trigger TOOL_USE patterns)

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "How is the system doing right now? I want to know about CPU and memory usage." | Agent should call system_metrics_snapshot tool. Classifies as CONVERSATIONAL (no TOOL_USE regex match for this phrasing), but LLM should still recognize the need for the tool. |
| 2 | "Is that normal for our setup?" | Follow-up interpreting the metrics. Should reference actual values from Turn 1. |

**Telemetry Assertions:**
- Turn 1: `task_type == "conversational"` (NOTE: "How is the system doing" doesn't match TOOL_USE patterns — this is a known gap in intent classification)
- Turn 1: `tool_call_completed` event for `system_metrics_snapshot` (tool usage independent of intent classification)
- Turn 1: Tool result includes `cpu_load_percent`, `mem_used_percent`, `disk_usage_percent`

**Quality Criteria:**
- [ ] Agent calls the tool (doesn't just describe what it would show)
- [ ] Response includes actual CPU %, memory %, and disk % values
- [ ] Values are interpreted, not just dumped (e.g., "CPU is at 45% which is moderate")
- [ ] Turn 2 provides context-aware interpretation
- [ ] GPU metrics included if Apple Silicon detected

**Evaluation Note:** The intent classification gap (natural language not triggering TOOL_USE) is itself a finding — log it for Slice 3 consideration.

---

### CP-22: Self-Telemetry Query

**Category:** Tools | **Targets:** self_telemetry_query tool for operational introspection
**Objective:** Verify that the agent can introspect its own operational health via self-telemetry

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "Show me your error rate and performance metrics from the last hour." | Agent should call self_telemetry_query with query_type="health" or "performance" and window="1h". |
| 2 | "Are there any specific errors I should be worried about?" | Agent should call self_telemetry_query with query_type="errors". |

**Telemetry Assertions:**
- Turn 1: `tool_call_completed` for `self_telemetry_query`
- Turn 1: Tool called with `query_type` in ("health", "performance") and `window == "1h"`
- Turn 2: `tool_call_completed` for `self_telemetry_query` with `query_type == "errors"`
- Both turns: Tool results include structured output (success_rate, latency stats, error counts)

**Quality Criteria:**
- [ ] Turn 1 reports success rate, latency percentiles, or throughput
- [ ] Turn 2 reports specific error types or confirms no errors
- [ ] Data is interpreted, not raw JSON dumped to the user
- [ ] Agent provides actionable guidance ("error rate is high, you may want to check...")
- [ ] Demonstrates genuine self-awareness about its own operational state

---

### CP-23: Search Memory Tool (Explicit)

**Category:** Tools | **Targets:** search_memory tool for explicit memory search requests
**Objective:** Verify that the agent uses the search_memory tool when explicitly asked to search memory

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "I've been learning about distributed systems, particularly consensus algorithms like Raft and Paxos." | Establishes context for memory. |
| 2 | "I'm also interested in how CRDTs enable conflict-free replication." | More context. |
| 3 | "Search your memory for anything related to distributed systems." | Should trigger search_memory tool. |

**Telemetry Assertions:**
- Turns 1-2: `task_type == "conversational"`
- Turn 3: `task_type == "tool_use"` (signal: `tool_intent_pattern`, matched by "Search...for")
- Turn 3: `search_memory_tool_called` event with `query_text` containing "distributed systems"
- Turn 3: `search_memory_tool_completed` event with `result_count >= 0`
- Turn 3: `query_path` in ("entity_match", "broad_recall") — depends on whether "anything" triggers broad path

**Quality Criteria:**
- [ ] Agent actually calls search_memory (not just recalling from session context)
- [ ] Results reference distributed systems topics from this conversation
- [ ] If memory service has prior data, surfaces relevant cross-session results
- [ ] If no prior memory data, gracefully indicates this and uses session context
- [ ] Response distinguishes between what's in memory vs. what was just discussed

---

## Category 7: Edge Cases (CP-24 to CP-25)

These paths test ambiguous or shifting scenarios where the classification system must make judgment calls.

---

### CP-24: Ambiguous Intent

**Category:** Edge Cases | **Targets:** Classification behavior when multiple patterns could match
**Objective:** Verify that the priority-ordered classification handles ambiguous messages correctly

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "Can you look into why our unit tests keep failing and fix the flaky ones in the authentication module?" | Multiple signals: "fix" + "unit test" → DELEGATION (coding patterns, priority 3). But also implies investigation (analysis-like). The priority order should resolve this to DELEGATION. |
| 2 | "Actually, before fixing anything, just analyze the failure patterns first." | Clearer intent: "analyze" → ANALYSIS (priority 5). Demonstrates that user can redirect. |

**Telemetry Assertions:**
- Turn 1: `task_type == "delegation"` (signal: `coding_pattern` — "fix" + "unit test" matches coding patterns, priority 3 beats analysis at priority 5)
- Turn 1: `confidence == 0.85`
- Turn 2: `task_type == "analysis"` (signal: `analysis_pattern`, matched by "analyze ")
- Turn 2: `confidence == 0.8`
- Note the priority resolution: coding (3) > analysis (5)

**Quality Criteria:**
- [ ] Turn 1: Agent treats this as a delegation/coding task (offers to fix or composes delegation package)
- [ ] Turn 2: Agent shifts to analysis mode — investigates patterns rather than implementing fixes
- [ ] Transition between intents is smooth, not jarring
- [ ] Agent doesn't carry over Turn 1's "fix" intent into Turn 2's "analyze" request

---

### CP-25: Intent Shift Mid-Conversation

**Category:** Edge Cases | **Targets:** Independent per-turn classification with no bleed-over
**Objective:** Verify that the gateway classifies each turn independently — prior turns don't bias current classification

| Turn | User Message | Expected Agent Behavior |
|------|-------------|------------------------|
| 1 | "Hey there, how are you doing today?" | Conversational greeting. |
| 2 | "Analyze the impact of adding a caching layer between our API and database." | Analysis request. Strategy should reflect analysis complexity. |
| 3 | "Write a function that implements a simple LRU cache in Python." | Delegation request. Completely different intent from Turn 2. |
| 4 | "What have we discussed about caching in this conversation?" | Memory recall. Should reference Turns 2 and 3. |

**Telemetry Assertions:**
- Turn 1: `task_type == "conversational"`, `strategy == "single"`
- Turn 2: `task_type == "analysis"`, `strategy == "single"` (simple analysis — 1 action verb "analyze", < 15 words)
- Turn 3: `task_type == "delegation"` (signal: `coding_pattern`, matched by "write a function" keyword)
- Turn 3: `strategy == "delegate"` (reason: `delegation_route_external`)
- Turn 4: `task_type == "memory_recall"` (signal: `memory_recall_pattern`, matched by "What have we discussed")
- Turn 4: `strategy == "single"` (reason: `memory_recall_always_single`)
- Each turn classified independently — no carry-over from prior turns

**Quality Criteria:**
- [ ] Each turn's response matches its intent (greeting, analysis, code, recall)
- [ ] Turn 2 provides genuine analysis, not just a one-line answer
- [ ] Turn 3 produces code (or delegation package), not more analysis
- [ ] Turn 4 recalls the caching discussion from Turns 2-3
- [ ] Conversation flow is natural despite the intent shifts
- [ ] No classification bleed-over (e.g., Turn 3 isn't treated as analysis just because Turn 2 was)

---

## Coverage Verification

### TaskType Coverage

| TaskType | Primary Path | Also Appears In |
|----------|-------------|-----------------|
| CONVERSATIONAL | CP-01 | CP-08, CP-12 T1-T2, CP-13 T1-T2, CP-14 T1-T2, CP-15 T1, CP-19, CP-21, CP-23 T1-T2, CP-25 T1 |
| MEMORY_RECALL | CP-02 | CP-12 T3, CP-13 T3, CP-14 T3, CP-25 T4 |
| ANALYSIS | CP-03 | CP-09, CP-10, CP-11 T2, CP-15 T2, CP-16, CP-17, CP-18, CP-25 T2 |
| PLANNING | CP-04 | — |
| DELEGATION | CP-05 | CP-24 T1, CP-25 T3 |
| SELF_IMPROVE | CP-06 | — |
| TOOL_USE | CP-07 | CP-23 T3 |

### DecompositionStrategy Coverage

| Strategy | Primary Path | Also Appears In |
|----------|-------------|-----------------|
| SINGLE | CP-08 | CP-01, CP-02, CP-03, CP-04, CP-06, CP-07, CP-11 T1/T3, CP-18 (under load) |
| HYBRID | CP-09 | CP-11 T2, CP-16, CP-18 (normal) |
| DECOMPOSE | CP-10 | CP-17 |
| DELEGATE | CP-05 | CP-24 T1, CP-25 T3 |

### Subsystem Coverage

| Subsystem | Paths |
|-----------|-------|
| Intent classification (regex) | CP-01 through CP-07, CP-24, CP-25 |
| Complexity estimation | CP-08 through CP-11 |
| Decomposition matrix | CP-08 through CP-11, CP-18 |
| Memory recall (targeted) | CP-12, CP-14 |
| Memory recall (broad) | CP-02, CP-13 |
| Memory-informed response | CP-15 |
| HYBRID expansion | CP-09, CP-11, CP-16, CP-18 |
| DECOMPOSE expansion | CP-10, CP-17 |
| Sub-agent synthesis | CP-16, CP-17 |
| Expansion budget | CP-18 |
| Context window trimming | CP-19, CP-20 |
| system_metrics_snapshot tool | CP-07 T2, CP-20 T3, CP-21 |
| self_telemetry_query tool | CP-20 T1-T2, CP-22 |
| search_memory tool | CP-23 |
| DelegationPackage composition | CP-05 |
| Per-turn independence | CP-11, CP-25 |
| Priority-ordered classification | CP-24 |

### Known Gaps (Not Covered in This Dataset)

These are intentionally excluded from the 25-path dataset. They may warrant separate evaluation:

| Gap | Reason | Suggested Follow-Up |
|-----|--------|---------------------|
| Cross-session memory recall | Requires consolidation pipeline to have run between sessions | Week 2 evaluation task |
| Episodic→semantic promotion | Requires multiple sessions + consolidation scheduler. **Note:** The Evaluation Phase Guide places this in Week 1 ("trigger at least one memory promotion"). To satisfy Week 1: run 5+ conversation paths from this dataset, then verify in Neo4j (`MATCH (e:Entity {memory_type: 'semantic'}) RETURN e`) that the consolidation scheduler promoted at least one entity. The promotion itself is not path-driven — it happens asynchronously via the brainstem scheduler after enough data accumulates. | Post-session verification (Week 1), deeper analysis (Week 2) |
| Insights engine patterns | Requires accumulated telemetry data (cost anomalies, delegation patterns) | Week 2-3 evaluation |
| MCP tool discovery | Depends on MCP servers being configured | Separate MCP integration test |
| DelegationOutcome recording | Requires external agent to complete delegation. The guide requests structured outcome logging: context sufficiency, rounds needed, missing context, satisfaction (1-5). Use this template per delegation: `{task_id, target_agent, context_sufficient: bool, rounds_needed: int, missing_context: [str], satisfaction: 1-5, notes: str}` | Manual evaluation with Claude Code |
| Governance mode restrictions | Requires ALERT/DEGRADED/LOCKDOWN modes active | Separate governance test |
| Captain's Log proposals | Requires self_telemetry + insights engine to produce proposals | Week 2-3 evaluation |
| Researcher's journal | The guide's "What to Watch For" section describes qualitative observations to record throughout evaluation (e.g., HYBRID overhead, memory irrelevance, delegation gaps). This is not a conversation path — it's an ongoing logging discipline. | Maintain `docs/research/EVALUATION_JOURNAL.md` per the guide |

---

## Automation Notes

### Can This Be Automated?

**Yes, partially.** The agent's HTTP API (`POST /chat`) accepts `message` and `session_id`. A Python test runner can:

1. Create a session via `POST /sessions`
2. Send each turn's message sequentially via `POST /chat`
3. Capture the response text + trace_id from the response
4. Query Elasticsearch (`agent-logs-*`) for telemetry events matching the trace_id
5. Assert telemetry values (task_type, complexity, strategy, tool calls)
6. Log response text for human quality evaluation

**What can be automated:**
- All telemetry assertions (intent, complexity, strategy, tool calls, expansion events)
- Tool call verification (was the right tool called with the right parameters?)
- Response structure checks (does it contain expected keywords?)

**What requires human judgment:**
- Response quality (is the analysis insightful? is the plan actionable?)
- Synthesis quality (does HYBRID output feel unified?)
- Memory coherence (does recall feel natural, not robotic?)
- Conversation flow (do intent shifts feel smooth?)

### Test Runner Sketch

```python
import httpx
import asyncio

async def run_conversation_path(path: ConversationPath) -> PathResult:
    async with httpx.AsyncClient(base_url="http://localhost:9000") as client:
        # Create session
        session = await client.post("/sessions")
        session_id = session.json()["session_id"]

        results = []
        for turn in path.turns:
            # Send message
            response = await client.post("/chat", json={
                "message": turn.user_message,
                "session_id": session_id,
            })
            data = response.json()

            # Query telemetry
            trace_id = data.get("trace_id")
            telemetry = await query_elasticsearch(trace_id)

            # Check assertions
            assertion_results = check_assertions(
                telemetry, turn.telemetry_assertions
            )

            results.append(TurnResult(
                response_text=data["response"],
                telemetry=telemetry,
                assertions=assertion_results,
            ))

        return PathResult(path_id=path.id, turns=results)
```

This sketch is not production code — it illustrates the automation pattern. A full implementation would be a Slice 3 or post-evaluation deliverable.

---

*This dataset should be revisited and updated as evaluation progresses. Record findings in `docs/research/EVALUATION_PHASE_FINDINGS.md`.*
