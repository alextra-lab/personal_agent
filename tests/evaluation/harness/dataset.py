"""All 35 evaluation conversation paths as Python data structures.

Each path mirrors the specification in docs/research/EVALUATION_DATASET.md.
Uses compact builder helpers from models.py for assertion definitions.

Organized by capability category:
- Category 1: Intent Classification (CP-01 to CP-07)
- Category 2: Decomposition Strategies (CP-08 to CP-11)
- Category 3: Memory System (CP-12 to CP-15)
- Category 4: Expansion & Sub-Agents (CP-16 to CP-18)
- Category 5: Context Management (CP-19 to CP-20)
- Category 6: Tools & Self-Inspection (CP-21 to CP-23)
- Category 7: Edge Cases (CP-24 to CP-25)
- Category 8: Memory Quality (CP-26 to CP-29)
"""

from __future__ import annotations

from tests.evaluation.harness.models import (
    ConversationPath,
    ConversationTurn,
    absent,
    fld,
    gte,
    neo4j_entity,
    neo4j_promoted,
    present,
)

# ============================================================================
# Category 1: Intent Classification (CP-01 to CP-07)
# ============================================================================

CP_01 = ConversationPath(
    path_id="CP-01",
    name="Conversational Intent",
    category="Intent Classification",
    objective=(
        "Verify that simple conversational messages fall through all "
        "pattern banks to the default CONVERSATIONAL classification"
    ),
    turns=(
        ConversationTurn(
            user_message="Hey, how's it going?",
            expected_behavior=("Responds conversationally. No tool calls. No sub-agents."),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                fld("intent_classified", "confidence", 0.7),
                fld("decomposition_assessed", "complexity", "simple"),
                fld("decomposition_assessed", "strategy", "single"),
                absent("tool_call_completed"),
                absent("hybrid_expansion_start"),
            ),
        ),
        ConversationTurn(
            user_message=("Tell me something interesting you've learned recently."),
            expected_behavior=(
                "Continues conversational tone. May draw on general knowledge. No tool calls."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                absent("tool_call_completed"),
            ),
        ),
    ),
    quality_criteria=(
        "Response is natural and engaging, not robotic",
        "Appropriate length (not a one-word answer, not an essay)",
        "No unnecessary tool invocations or system introspection",
        "Turn 2 response demonstrates personality or knowledge",
    ),
)

CP_02 = ConversationPath(
    path_id="CP-02",
    name="Memory Recall Intent",
    category="Intent Classification",
    objective=(
        "Verify that 'have we discussed' triggers MEMORY_RECALL classification and broad recall"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "I've been thinking about building a recommendation "
                "engine using collaborative filtering."
            ),
            expected_behavior=(
                "Responds to the topic. Entities like 'recommendation "
                "engine' and 'collaborative filtering' should be captured."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=("What have we discussed in our conversations so far?"),
            expected_behavior=(
                "Triggers memory recall. Should reference the "
                "recommendation engine topic from Turn 1."
            ),
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                fld("intent_classified", "confidence", 0.9),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 2 response references the recommendation engine topic",
        "If no prior history, gracefully acknowledges limited history",
        "Response is structured (not a wall of text)",
        "Does not hallucinate conversations that never happened",
    ),
)

CP_03 = ConversationPath(
    path_id="CP-03",
    name="Analysis Intent",
    category="Intent Classification",
    objective="Verify that 'Analyze' triggers ANALYSIS classification",
    turns=(
        ConversationTurn(
            user_message=(
                "Analyze the trade-offs between REST and GraphQL "
                "for a small team building internal APIs."
            ),
            expected_behavior=(
                "Provides structured analysis comparing REST vs GraphQL. "
                "Addresses team size constraint."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("intent_classified", "confidence", 0.8),
                fld("decomposition_assessed", "complexity", "simple"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message="Which would you lean toward for our case and why?",
            expected_behavior=("Provides a recommendation grounded in the prior analysis."),
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
    ),
    quality_criteria=(
        "Turn 1 covers at least 3 trade-off dimensions",
        "Addresses the 'small team' constraint specifically",
        "Turn 2 recommendation is consistent with Turn 1 analysis",
        "Structured format (bullets, headers, or numbered points)",
    ),
)

CP_04 = ConversationPath(
    path_id="CP-04",
    name="Planning Intent",
    category="Intent Classification",
    objective="Verify that 'Plan' triggers PLANNING classification",
    turns=(
        ConversationTurn(
            user_message=("Plan the next steps for adding user authentication to our API service."),
            expected_behavior=(
                "Produces a structured plan with discrete steps, "
                "rough ordering, and considerations."
            ),
            assertions=(
                fld("intent_classified", "task_type", "planning"),
                fld("intent_classified", "confidence", 0.8),
                fld("decomposition_assessed", "complexity", "simple"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=("What should we tackle first, and what can we defer?"),
            expected_behavior=("Prioritizes the steps with reasoning."),
            assertions=(),
        ),
    ),
    quality_criteria=(
        "Plan includes at least 4 concrete steps",
        "Steps have a logical ordering",
        "Addresses auth method choices (OAuth, JWT, session-based)",
        "Turn 2 provides clear prioritization with reasoning",
    ),
)

CP_05 = ConversationPath(
    path_id="CP-05",
    name="Delegation Intent (Explicit and Implicit)",
    category="Intent Classification",
    objective=(
        "Verify both explicit delegation ('Use Claude Code to...') and "
        "implicit delegation ('Write a function...') trigger DELEGATION"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Use Claude Code to write a function that parses nested "
                "JSON configuration files with schema validation and "
                "returns structured error messages for each validation "
                "failure."
            ),
            expected_behavior=(
                "Classifies as DELEGATION. Should compose a "
                "DelegationPackage with target_agent='claude-code'."
            ),
            assertions=(
                fld("intent_classified", "task_type", "delegation"),
                fld("intent_classified", "confidence", 0.85),
                fld("decomposition_assessed", "strategy", "delegate"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Write unit tests for the edge cases — circular "
                "references, missing required keys, and deeply nested "
                "structures beyond 10 levels."
            ),
            expected_behavior=("Follow-up delegation. Enriches the task with test requirements."),
            assertions=(fld("intent_classified", "task_type", "delegation"),),
        ),
        ConversationTurn(
            user_message=(
                "What context would you include in the handoff to make "
                "sure Claude Code doesn't need to ask follow-up questions?"
            ),
            expected_behavior=(
                "Explains DelegationPackage contents: relevant_files, conventions, known_pitfalls."
            ),
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
    ),
    quality_criteria=(
        "Turn 1: Agent composes a DelegationPackage rather than writing code",
        "Turn 1: task_description is clear for an agent with no prior context",
        "Turn 2: acceptance_criteria includes the three edge cases",
        "Turn 3: Demonstrates awareness of what external agents need",
        "Package is sufficient for Claude Code without follow-up questions",
    ),
)

CP_06 = ConversationPath(
    path_id="CP-06",
    name="Self-Improvement Intent",
    category="Intent Classification",
    objective=(
        "Verify that self-referential improvement questions trigger SELF_IMPROVE classification"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "What improvements would you suggest to your own memory and recall system?"
            ),
            expected_behavior=("Discusses potential improvements to its own architecture."),
            assertions=(
                fld("intent_classified", "task_type", "self_improve"),
                fld("intent_classified", "confidence", 0.85),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=("Which of those would have the biggest impact on your usefulness to me?"),
            expected_behavior="Prioritizes suggestions with reasoning.",
            assertions=(),
        ),
    ),
    quality_criteria=(
        "Suggestions reference actual system capabilities",
        "Does not hallucinate features the system doesn't have",
        "Turn 2 prioritization is grounded, not generic",
        "Demonstrates self-awareness about current limitations",
    ),
)

CP_07 = ConversationPath(
    path_id="CP-07",
    name="Tool Use Intent",
    category="Intent Classification",
    objective=("Verify that explicit tool-use language triggers TOOL_USE classification"),
    turns=(
        ConversationTurn(
            user_message="List the tools you currently have access to.",
            expected_behavior=(
                "Enumerates available tools (search_memory, "
                "system_metrics_snapshot, self_telemetry_query, "
                "read_file, list_directory, plus any MCP tools)."
            ),
            assertions=(
                fld("intent_classified", "task_type", "tool_use"),
                fld("intent_classified", "confidence", 0.8),
                fld("decomposition_assessed", "complexity", "simple"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=("Read the system log and tell me if anything looks concerning."),
            expected_behavior=("Calls self_telemetry_query or reads log output. Reports findings."),
            assertions=(
                fld("intent_classified", "task_type", "tool_use"),
                present("tool_call_completed"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 1 lists tools accurately",
        "Turn 2 actually calls a tool (not just describes it)",
        "Tool results are interpreted and summarized, not dumped raw",
        "If system is healthy, says so; if issues found, highlights them",
    ),
)

# ============================================================================
# Category 2: Decomposition Strategies (CP-08 to CP-11)
# ============================================================================

CP_08 = ConversationPath(
    path_id="CP-08",
    name="SINGLE Strategy (Simple Question)",
    category="Decomposition Strategies",
    objective=("Verify that a simple, short question results in SINGLE strategy"),
    turns=(
        ConversationTurn(
            user_message="What is dependency injection?",
            expected_behavior=("Clear, concise explanation. No sub-agents. Single LLM call."),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                fld("decomposition_assessed", "complexity", "simple"),
                fld("decomposition_assessed", "strategy", "single"),
                absent("hybrid_expansion_start"),
            ),
        ),
        ConversationTurn(
            user_message="Can you give me a quick example in Python?",
            expected_behavior="Another simple response. Still SINGLE.",
            assertions=(
                fld("decomposition_assessed", "strategy", "single"),
                absent("hybrid_expansion_start"),
            ),
        ),
    ),
    quality_criteria=(
        "Explanation is clear and accurate",
        "Appropriate depth for a definitional question",
        "Python example in Turn 2 is correct and illustrative",
        "Fast response time (no expansion overhead)",
    ),
)

CP_09 = ConversationPath(
    path_id="CP-09",
    name="HYBRID Strategy (Moderate Analysis)",
    category="Decomposition Strategies",
    objective=(
        "Verify that a moderate-complexity analysis triggers HYBRID with sub-agent expansion"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Research the advantages of event sourcing versus CRUD "
                "for session storage, and evaluate their suitability "
                "for a PostgreSQL-backed system."
            ),
            expected_behavior=(
                "HYBRID expansion triggered. Sub-agents research "
                "individual aspects. Final response synthesizes."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "complexity", "moderate"),
                fld("decomposition_assessed", "strategy", "hybrid"),
                present("hybrid_expansion_start"),
                gte("hybrid_expansion_start", "sub_agent_count", 1),
                present("hybrid_expansion_complete"),
                gte("hybrid_expansion_complete", "successes", 1),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Given what you found, which approach would you recommend for our use case?"
            ),
            expected_behavior=("Single follow-up referencing Turn 1 analysis."),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
    ),
    quality_criteria=(
        "Response covers both event sourcing AND CRUD approaches",
        "PostgreSQL-specific considerations addressed",
        "Sub-agent contributions synthesized coherently",
        "Turn 2 recommendation grounded in Turn 1 analysis",
        "Quality noticeably better than a single-pass response",
    ),
)

CP_10 = ConversationPath(
    path_id="CP-10",
    name="DECOMPOSE Strategy (Complex Multi-Part Analysis)",
    category="Decomposition Strategies",
    objective=("Verify that a complex multi-part request with 3+ action verbs triggers DECOMPOSE"),
    turns=(
        ConversationTurn(
            user_message=(
                "Compare three approaches to distributed caching, "
                "evaluate their performance under load, analyze the "
                "cost implications for each, and recommend which fits "
                "a system handling ten thousand requests per second."
            ),
            expected_behavior=(
                "Full decomposition. Multiple sub-agents. Comprehensive synthesized output."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "complexity", "complex"),
                fld("decomposition_assessed", "strategy", "decompose"),
                present("hybrid_expansion_start"),
                gte("hybrid_expansion_start", "sub_agent_count", 2),
                gte("hybrid_expansion_complete", "successes", 2),
            ),
        ),
    ),
    quality_criteria=(
        "At least 3 caching approaches compared",
        "Performance evaluation includes metrics or benchmarks",
        "Cost analysis is concrete, not vague",
        "Recommendation is specific with clear reasoning",
        "Response well-structured with sections for each part",
    ),
)

CP_11 = ConversationPath(
    path_id="CP-11",
    name="Complexity Escalation Across Turns",
    category="Decomposition Strategies",
    objective=(
        "Verify that each turn is classified independently — "
        "a simple first question doesn't lock the strategy"
    ),
    turns=(
        ConversationTurn(
            user_message="What is a knowledge graph?",
            expected_behavior="Simple definitional answer. SINGLE strategy.",
            assertions=(
                fld("decomposition_assessed", "complexity", "simple"),
                fld("decomposition_assessed", "strategy", "single"),
                absent("hybrid_expansion_start"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Compare Neo4j and Dgraph for entity storage, and "
                "evaluate their query performance and Python ecosystem "
                "support."
            ),
            expected_behavior=("Moderate analysis. HYBRID strategy. Sub-agents spawned."),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "complexity", "moderate"),
                fld("decomposition_assessed", "strategy", "hybrid"),
                present("hybrid_expansion_start"),
                gte("hybrid_expansion_start", "sub_agent_count", 1),
            ),
        ),
        ConversationTurn(
            user_message="Based on that comparison, which should we use?",
            expected_behavior="Simple follow-up. Back to SINGLE strategy.",
            assertions=(
                fld("decomposition_assessed", "complexity", "simple"),
                fld("decomposition_assessed", "strategy", "single"),
                absent("hybrid_expansion_start"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 1 is concise and accurate",
        "Turn 2 is noticeably more detailed (HYBRID effect)",
        "Turn 2 covers both databases across both dimensions",
        "Turn 3 recommendation references Turn 2 analysis",
        "No classification bleed-over between turns",
    ),
)

# ============================================================================
# Category 3: Memory System (CP-12 to CP-15)
# ============================================================================

CP_12 = ConversationPath(
    path_id="CP-12",
    name="Entity Seeding and Targeted Recall",
    category="Memory System",
    objective=("Verify that entities mentioned in conversation are captured and can be recalled"),
    turns=(
        ConversationTurn(
            user_message=(
                "I've been working on a project called Project Atlas. "
                "It's a data pipeline that processes satellite imagery "
                "using Apache Kafka and Apache Spark."
            ),
            expected_behavior=(
                "Responds to the topic. Entities captured: "
                "Project Atlas, Apache Kafka, Apache Spark."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "The team lead is Maria Chen and we're deploying to AWS "
                "with a target of processing 500 images per hour."
            ),
            expected_behavior=("More context. Entities: Maria Chen, AWS."),
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message="What do you know about Project Atlas?",
            expected_behavior=(
                "Triggers MEMORY_RECALL. Should reference the data "
                "pipeline, Kafka, Spark, Maria Chen, and AWS."
            ),
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                fld("intent_classified", "confidence", 0.9),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 3 references Project Atlas by name",
        "Mentions at least 3 of: pipeline, imagery, Kafka, Spark, Maria Chen, AWS",
        "Information is accurate (not hallucinated)",
        "Demonstrates synthesis, not just parroting",
    ),
)

CP_13 = ConversationPath(
    path_id="CP-13",
    name="Broad Recall",
    category="Memory System",
    objective=(
        "Verify that open-ended recall questions trigger the broad "
        "recall path and return grouped results"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "I've been evaluating Django and FastAPI for our new "
                "web service. FastAPI seems faster but Django has more "
                "batteries included."
            ),
            expected_behavior="Responds to the framework comparison.",
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message=(
                "We also need to decide between PostgreSQL and MongoDB "
                "for the storage layer. Our data is mostly relational "
                "but we have some document-like structures."
            ),
            expected_behavior="Responds to the database discussion.",
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message="What topics have we covered in this conversation?",
            expected_behavior=(
                "MEMORY_RECALL with broad recall. Lists both the framework and database topics."
            ),
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
    ),
    quality_criteria=(
        "Identifies at least 2 distinct topics (web frameworks, databases)",
        "Mentions specific technologies (Django, FastAPI, PostgreSQL, MongoDB)",
        "Response is organized — groups topics",
        "Captures key considerations (speed vs batteries, relational vs document)",
        "Does not hallucinate topics not discussed",
    ),
)

CP_14 = ConversationPath(
    path_id="CP-14",
    name="Multi-Entity Tracking",
    category="Memory System",
    objective=(
        "Verify that when multiple entities are introduced, the agent recalls the correct one"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Alice on our team is building a CI/CD automation tool "
                "called BuildBot. She's using Python and GitHub Actions."
            ),
            expected_behavior="Responds about Alice and BuildBot.",
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message=(
                "Bob is working on a deployment tool called DeployTool. "
                "He's focused on Terraform and AWS infrastructure."
            ),
            expected_behavior="Responds about Bob and DeployTool.",
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message="What do you know about Alice and her work?",
            expected_behavior=(
                "Recalls Alice + BuildBot + Python + GitHub Actions. "
                "Should NOT conflate with Bob's work."
            ),
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                fld("intent_classified", "confidence", 0.9),
            ),
        ),
    ),
    quality_criteria=(
        "Correctly associates Alice with BuildBot, Python, GitHub Actions",
        "Does NOT mention Bob, DeployTool, Terraform, or AWS",
        "Demonstrates entity-relationship awareness",
        "Clean separation between the two people",
    ),
)

CP_15 = ConversationPath(
    path_id="CP-15",
    name="Memory-Informed Response",
    category="Memory System",
    objective=("Verify that earlier context shapes later responses, not just generic knowledge"),
    turns=(
        ConversationTurn(
            user_message=(
                "I'm building a real-time dashboard using WebSockets "
                "and React to monitor IoT sensor data produced by "
                "industrial equipment."
            ),
            expected_behavior="Acknowledges the project details.",
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message=(
                "What technology stack would you recommend for the backend of this project?"
            ),
            expected_behavior=(
                "Recommendations compatible with WebSockets, IoT, "
                "and real-time requirements. Not a generic answer."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
    ),
    quality_criteria=(
        "Recommendation explicitly references WebSockets from Turn 1",
        "Addresses IoT/real-time requirements (not generic web stack)",
        "Technologies compatible with stated stack",
        "Does not recommend conflicting technologies",
        "Feels like a conversation, not two isolated questions",
    ),
)

# ============================================================================
# Category 4: Expansion & Sub-Agents (CP-16 to CP-18)
# ============================================================================

CP_16 = ConversationPath(
    path_id="CP-16",
    name="HYBRID Synthesis Quality",
    category="Expansion & Sub-Agents",
    objective=(
        "Verify that HYBRID expansion produces a synthesized response "
        "better than a single-pass answer"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Research microservices communication patterns and "
                "evaluate the trade-offs between synchronous HTTP, "
                "asynchronous messaging, and gRPC."
            ),
            expected_behavior=(
                "HYBRID expansion triggered. Sub-agents research "
                "different patterns. Primary agent synthesizes."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "complexity", "moderate"),
                fld("decomposition_assessed", "strategy", "hybrid"),
                present("hybrid_expansion_start"),
                gte("hybrid_expansion_start", "sub_agent_count", 1),
                present("hybrid_expansion_complete"),
                gte("hybrid_expansion_complete", "successes", 1),
                present("planner_started"),          # Layer 2: planner was invoked
                present("expansion_dispatch_started"),  # Layer 2: sub-agents dispatched
            ),
        ),
        ConversationTurn(
            user_message=(
                "Which pattern would you recommend for a system with "
                "both low-latency and high-throughput requirements?"
            ),
            expected_behavior=("Follow-up referencing Turn 1 analysis. SINGLE strategy."),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
    ),
    quality_criteria=(
        "All three communication patterns covered (HTTP, async, gRPC)",
        "Trade-offs are concrete (latency, complexity, tooling)",
        "Response feels unified — not three stitched answers",
        "Synthesis adds value (comparison table, decision framework)",
        "Turn 2 recommendation grounded in Turn 1 analysis",
    ),
)

CP_17 = ConversationPath(
    path_id="CP-17",
    name="Sub-Agent Concurrency",
    category="Expansion & Sub-Agents",
    objective=("Verify that DECOMPOSE spawns multiple sub-agents and synthesizes all results"),
    turns=(
        ConversationTurn(
            user_message=(
                "Compare the performance characteristics of Redis, "
                "Memcached, and Hazelcast for distributed caching. "
                "Analyze their memory management approaches and "
                "evaluate operational complexity. Recommend which "
                "fits our workload of ten thousand requests per second."
            ),
            expected_behavior=(
                "DECOMPOSE triggered. Multiple sub-agents. All results synthesized."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "complexity", "complex"),
                fld("decomposition_assessed", "strategy", "decompose"),
                present("hybrid_expansion_start"),
                gte("hybrid_expansion_start", "sub_agent_count", 2),
                gte("hybrid_expansion_complete", "successes", 2),
                present("planner_started"),
                present("expansion_dispatch_started"),
                absent("user_visible_timeout"),  # Layer 4: no raw timeout to user
            ),
        ),
    ),
    quality_criteria=(
        "All three caching systems compared",
        "Performance includes throughput, latency, memory efficiency",
        "Memory management differences explained",
        "Operational complexity addressed",
        "Final recommendation is specific and justified",
    ),
)

CP_18 = ConversationPath(
    path_id="CP-18",
    name="Expansion Budget Enforcement",
    category="Expansion & Sub-Agents",
    objective=("Verify that expansion_budget forces SINGLE under resource pressure"),
    setup_notes=(
        "Requires system resource pressure. Before running:\n"
        "1. Run `stress --cpu 4 --timeout 60s` to push CPU above 70%\n"
        "2. OR set expansion_budget=0 in governance for testing\n"
        "3. Monitor expansion_budget_computed events\n"
        "Run same message WITHOUT load as control comparison."
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Research the advantages of container orchestration "
                "and evaluate Kubernetes versus Docker Swarm for "
                "small engineering teams."
            ),
            expected_behavior=(
                "Normally HYBRID (2 action verbs, ANALYSIS). Under "
                "resource pressure, forced to SINGLE."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                # Under load: strategy forced to single
                # Under normal: strategy would be hybrid
                # Assert on what's expected given the test conditions
            ),
        ),
    ),
    quality_criteria=(
        "Under load: provides reasonable response (graceful degradation)",
        "Under load: response less detailed than HYBRID version",
        "Budget enforcement transparent in telemetry",
        "Compare quality: SINGLE vs HYBRID version of same question",
    ),
)

# ============================================================================
# Category 5: Context Management (CP-19 to CP-20)
# ============================================================================

CP_19 = ConversationPath(
    path_id="CP-19",
    name="Long Conversation Trimming",
    category="Context Management",
    objective=(
        "Verify that long conversations are trimmed intelligently — important context preserved"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Let's talk about our system architecture. We use a "
                "microservices pattern with FastAPI services "
                "communicating over HTTP."
            ),
            expected_behavior="Establishes foundational context.",
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message=("Our primary database is PostgreSQL for transactional data."),
            expected_behavior="Adds more context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "We also use Elasticsearch for logging and Neo4j for our knowledge graph."
            ),
            expected_behavior="More context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "The deployment is on Docker Compose locally and Kubernetes in production."
            ),
            expected_behavior="More context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=("We've been having issues with service discovery between containers."),
            expected_behavior="Introduces a problem.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=("I tried using Consul but it added too much operational overhead."),
            expected_behavior="Adds history.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "We're now evaluating DNS-based service discovery versus Envoy sidecar proxies."
            ),
            expected_behavior="Current state.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "The team is leaning toward Envoy because it also handles load balancing."
            ),
            expected_behavior="Team preference.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "But I'm worried about the memory overhead of running "
                "Envoy sidecars on every service."
            ),
            expected_behavior="Concern.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=("Going back to the beginning — what was our primary database again?"),
            expected_behavior=("Should still know PostgreSQL despite potential context trimming."),
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                present("context_budget_applied"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 10: correctly identifies PostgreSQL as primary database",
        "If trimmed, important foundational facts were retained",
        "Conversation feels coherent throughout",
        "Agent doesn't forget mid-conversation",
        "context_budget_applied event fires on Turn 10 with correct trimmed/overflow_action fields",
    ),
)

CP_19_V2 = ConversationPath(
    path_id="CP-19-v2",
    name="Implicit Recall — 'again' cue",
    category="Context Management",
    objective="Verify recall controller catches 'again' backward-reference",
    turns=(
        ConversationTurn(
            user_message="We need to pick a primary database for the project. Let's go with PostgreSQL.",
            expected_behavior="Acknowledges PostgreSQL choice",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message="Now let's discuss the API framework. We should use FastAPI.",
            expected_behavior="Acknowledges FastAPI choice",
            assertions=(),
        ),
        ConversationTurn(
            user_message="What was our primary database again?",
            expected_behavior="Recalls PostgreSQL from earlier in session",
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                present("recall_cue_detected"),
            ),
        ),
    ),
    quality_criteria=(
        "Correctly identifies PostgreSQL as primary database",
        "Recall controller reclassifies from CONVERSATIONAL to MEMORY_RECALL",
        "Agent does not claim ignorance or ask user to repeat",
    ),
)

CP_19_V3 = ConversationPath(
    path_id="CP-19-v3",
    name="Implicit Recall — 'earlier' cue",
    category="Context Management",
    objective="Verify recall controller catches 'earlier' backward-reference",
    turns=(
        ConversationTurn(
            user_message="We decided to use Redis for our caching layer.",
            expected_behavior="Acknowledges Redis choice",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message="Let's move on to discussing monitoring.",
            expected_behavior="Topic shift",
            assertions=(),
        ),
        ConversationTurn(
            user_message="Going back to earlier — what caching system did we pick?",
            expected_behavior="Recalls Redis from session",
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                present("recall_cue_detected"),
            ),
        ),
    ),
    quality_criteria=(
        "Correctly identifies Redis as caching system",
        "Recall controller reclassifies from CONVERSATIONAL to MEMORY_RECALL",
    ),
)

CP_19_V4 = ConversationPath(
    path_id="CP-19-v4",
    name="Implicit Recall — 'remind me' cue",
    category="Context Management",
    objective="Verify recall controller catches 'remind me' cue",
    turns=(
        ConversationTurn(
            user_message="For the message queue, let's use RabbitMQ.",
            expected_behavior="Acknowledges RabbitMQ choice",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message="Actually, let's also consider the deployment strategy.",
            expected_behavior="Topic shift",
            assertions=(),
        ),
        ConversationTurn(
            user_message="Remind me what we decided on the message queue?",
            expected_behavior="Recalls RabbitMQ from session",
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                present("recall_cue_detected"),
            ),
        ),
    ),
    quality_criteria=(
        "Correctly identifies RabbitMQ as message queue",
        "Recall controller reclassifies from CONVERSATIONAL to MEMORY_RECALL",
    ),
)

CP_19_V5 = ConversationPath(
    path_id="CP-19-v5",
    name="Implicit Recall — 'what did we decide' cue",
    category="Context Management",
    objective="Verify recall controller catches 'what did we decide' cue",
    turns=(
        ConversationTurn(
            user_message="For the CI/CD pipeline, we should go with GitHub Actions.",
            expected_behavior="Acknowledges GitHub Actions choice",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message="Let me also think about the testing strategy.",
            expected_behavior="Topic shift",
            assertions=(),
        ),
        ConversationTurn(
            user_message="What did we decide on the CI/CD pipeline?",
            expected_behavior="Recalls GitHub Actions from session",
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                present("recall_cue_detected"),
            ),
        ),
    ),
    quality_criteria=(
        "Correctly identifies GitHub Actions as CI/CD choice",
        "Recall controller reclassifies from CONVERSATIONAL to MEMORY_RECALL",
    ),
)

CP_19_V6 = ConversationPath(
    path_id="CP-19-v6",
    name="Implicit Recall — 'refresh my memory' cue",
    category="Context Management",
    objective="Verify recall controller catches 'refresh my memory' cue",
    turns=(
        ConversationTurn(
            user_message="Our main programming language will be Python 3.12.",
            expected_behavior="Acknowledges Python choice",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message="We also need a frontend framework. Let's use React.",
            expected_behavior="Acknowledges React",
            assertions=(),
        ),
        ConversationTurn(
            user_message="Refresh my memory — what was our main programming language?",
            expected_behavior="Recalls Python 3.12 from session",
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                present("recall_cue_detected"),
            ),
        ),
    ),
    quality_criteria=(
        "Correctly identifies Python 3.12 as main language",
        "Recall controller reclassifies from CONVERSATIONAL to MEMORY_RECALL",
    ),
)

CP_19_V7 = ConversationPath(
    path_id="CP-19-v7",
    name="Implicit Recall — 'the X we discussed' cue",
    category="Context Management",
    objective="Verify recall controller catches 'the X we discussed' resumptive reference",
    turns=(
        ConversationTurn(
            user_message="We should use Terraform for infrastructure as code.",
            expected_behavior="Acknowledges Terraform choice",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message="Let's also set up monitoring with Grafana.",
            expected_behavior="Topic shift",
            assertions=(),
        ),
        ConversationTurn(
            user_message="The tool we discussed earlier — can you confirm what it was?",
            expected_behavior="Recalls Terraform from session",
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                present("recall_cue_detected"),
            ),
        ),
    ),
    quality_criteria=(
        "Correctly identifies Terraform as infrastructure tool",
        "Recall controller reclassifies from CONVERSATIONAL to MEMORY_RECALL",
    ),
)

CP_20 = ConversationPath(
    path_id="CP-20",
    name="Progressive Token Budget Management",
    category="Context Management",
    objective=("Verify that tool-heavy conversations manage token budgets correctly"),
    turns=(
        ConversationTurn(
            user_message="Run the system health check.",
            expected_behavior=("Calls self_telemetry_query(health). Large tool output."),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                present("tool_call_completed"),
            ),
        ),
        ConversationTurn(
            user_message="Now show me the recent error details.",
            expected_behavior=("Calls self_telemetry_query(errors). More tool output."),
            assertions=(present("tool_call_completed"),),
        ),
        ConversationTurn(
            user_message="Also check the system metrics.",
            expected_behavior=("Calls system_metrics_snapshot. Even more tool output."),
            assertions=(present("tool_call_completed"),),
        ),
        ConversationTurn(
            user_message=("Summarize everything you've found — is the system healthy overall?"),
            expected_behavior=("Synthesizes all three tool results. Context may need trimming."),
            assertions=(present("context_budget_applied"),),
        ),
    ),
    quality_criteria=(
        "Each tool call returns valid data",
        "Turn 4 synthesizes findings coherently",
        "If trimmed, most recent tool results preserved",
        "Agent identifies any genuine issues",
        "context_budget_applied event fires on Turn 4 with correct trimmed/overflow_action fields",
    ),
)

# ============================================================================
# Category 6: Tools & Self-Inspection (CP-21 to CP-23)
# ============================================================================

CP_21 = ConversationPath(
    path_id="CP-21",
    name="System Metrics (Natural Language)",
    category="Tools & Self-Inspection",
    objective=(
        "Verify the agent calls system_metrics_snapshot even when "
        "intent is CONVERSATIONAL (natural language doesn't match "
        "TOOL_USE patterns)"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "How is the system doing right now? I want to know about CPU and memory usage."
            ),
            expected_behavior=(
                "Calls system_metrics_snapshot tool despite CONVERSATIONAL classification."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                present("tool_call_completed"),
            ),
        ),
        ConversationTurn(
            user_message="Is that normal for our setup?",
            expected_behavior=("Interprets metrics with context."),
            assertions=(),
        ),
    ),
    quality_criteria=(
        "Agent calls the tool (doesn't just describe it)",
        "Response includes actual CPU %, memory %, disk % values",
        "Values are interpreted, not just dumped",
        "Turn 2 provides context-aware interpretation",
    ),
)

CP_22 = ConversationPath(
    path_id="CP-22",
    name="Self-Telemetry Query",
    category="Tools & Self-Inspection",
    objective=("Verify the agent can introspect its own operational health"),
    turns=(
        ConversationTurn(
            user_message=("Show me your error rate and performance metrics over the past hour."),
            expected_behavior=(
                "Calls self_telemetry_query with query_type='health' "
                "or 'performance' and window='1h'."
            ),
            assertions=(present("tool_call_completed"),),
        ),
        ConversationTurn(
            user_message=("Are there any specific errors I should be worried about?"),
            expected_behavior=("Calls self_telemetry_query with query_type='errors'."),
            assertions=(present("tool_call_completed"),),
        ),
    ),
    quality_criteria=(
        "Turn 1 reports success rate, latency, or throughput",
        "Turn 2 reports specific error types or confirms no errors",
        "Data is interpreted, not raw JSON dumped",
        "Demonstrates genuine self-awareness about operational state",
    ),
)

CP_23 = ConversationPath(
    path_id="CP-23",
    name="Search Memory Tool (Explicit)",
    category="Tools & Self-Inspection",
    objective=("Verify that the agent uses the search_memory tool when explicitly asked"),
    turns=(
        ConversationTurn(
            user_message=(
                "I've been learning about distributed systems, "
                "particularly consensus algorithms like Raft and Paxos."
            ),
            expected_behavior="Establishes context for memory.",
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message=("I'm also interested in how CRDTs enable conflict-free replication."),
            expected_behavior="More context.",
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message=("Search your memory for anything related to distributed systems."),
            expected_behavior=("Triggers search_memory tool."),
            assertions=(
                fld("intent_classified", "task_type", "tool_use"),
                present("tool_call_completed"),
            ),
        ),
    ),
    quality_criteria=(
        "Agent actually calls search_memory tool",
        "Results reference distributed systems topics",
        "If no prior data, gracefully indicates this",
        "Distinguishes memory data vs. session context",
    ),
)

# ============================================================================
# Category 7: Edge Cases (CP-24 to CP-25)
# ============================================================================

CP_24 = ConversationPath(
    path_id="CP-24",
    name="Ambiguous Intent",
    category="Edge Cases",
    objective=("Verify that priority-ordered classification handles ambiguous messages correctly"),
    turns=(
        ConversationTurn(
            user_message=(
                "Can you look into why our unit tests keep failing "
                "and fix the flaky ones in the authentication module?"
            ),
            expected_behavior=(
                "Multiple signals: 'fix' + 'unit test' → DELEGATION "
                "(priority 3 beats analysis at priority 5)."
            ),
            assertions=(
                fld("intent_classified", "task_type", "delegation"),
                fld("intent_classified", "confidence", 0.85),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Actually, before fixing anything, just analyze the failure patterns first."
            ),
            expected_behavior=(
                "Clearer intent: 'analyze' → ANALYSIS. Demonstrates user can redirect."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("intent_classified", "confidence", 0.8),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 1: treats as delegation/coding task",
        "Turn 2: shifts to analysis mode — investigates patterns",
        "Transition between intents is smooth",
        "No carry-over of Turn 1 intent into Turn 2",
    ),
)

CP_25 = ConversationPath(
    path_id="CP-25",
    name="Intent Shift Mid-Conversation",
    category="Edge Cases",
    objective=(
        "Verify that the gateway classifies each turn independently "
        "— no bleed-over from prior turns"
    ),
    turns=(
        ConversationTurn(
            user_message="Hey there, how are you doing today?",
            expected_behavior="Conversational greeting.",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Analyze the impact of adding a caching layer between our API and database."
            ),
            expected_behavior="Analysis request.",
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=("Write a function that implements a simple LRU cache in Python."),
            expected_behavior=("Delegation request. Different intent from Turn 2."),
            assertions=(
                fld("intent_classified", "task_type", "delegation"),
                fld("decomposition_assessed", "strategy", "delegate"),
            ),
        ),
        ConversationTurn(
            user_message=("What have we discussed about caching in this conversation?"),
            expected_behavior=("Memory recall. References Turns 2 and 3."),
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
    ),
    quality_criteria=(
        "Each turn's response matches its intent",
        "Turn 2 provides genuine analysis",
        "Turn 3 produces code (or delegation package)",
        "Turn 4 recalls the caching discussion from Turns 2-3",
        "No classification bleed-over between turns",
    ),
)

# ============================================================================
# Category 8: Memory Quality (CP-26 to CP-29)
# ============================================================================

CP_26 = ConversationPath(
    path_id="CP-26",
    name="Memory Promotion Quality",
    category="Memory Quality",
    objective=(
        "Verify that entities seeded across multiple turns are extracted, "
        "stored in Neo4j, and promoted to semantic memory with accurate facts"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "I'm building a service called DataForge. It uses Apache Flink "
                "for stream processing and stores results in ClickHouse."
            ),
            expected_behavior=(
                "Responds to the topic. Entities captured: DataForge, Apache Flink, ClickHouse."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "The project lead is Priya Sharma. We're targeting "
                "a throughput of 50,000 events per second on GCP."
            ),
            expected_behavior=("More context. Entities: Priya Sharma, GCP."),
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message=(
                "DataForge also integrates with Grafana for real-time "
                "monitoring and uses Kafka as the ingestion layer "
                "before Flink processes the data."
            ),
            expected_behavior=(
                "Third turn enriching the entity graph: "
                "Grafana, Kafka, reinforces Flink connection."
            ),
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message="What do you remember about the DataForge project?",
            expected_behavior=(
                "Memory recall. Should reference DataForge, Flink, "
                "ClickHouse, Priya Sharma, GCP, Grafana, and Kafka."
            ),
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                fld("decomposition_assessed", "strategy", "single"),
                present("memory_enrichment_completed"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 4 references DataForge by name",
        "Mentions at least 5 of: Flink, ClickHouse, Priya Sharma, GCP, Grafana, Kafka",
        "Information is accurate (no hallucinated technologies or people)",
        "Demonstrates entity-relationship awareness (Kafka -> Flink -> ClickHouse pipeline)",
        "Does not confuse entities from other conversations",
    ),
    setup_notes=(
        "Requires consolidation scheduler to have run after entity seeding.\n"
        "The post_path_delay_s (default 5s) plus Neo4jChecker retries (4x3s)\n"
        "allow up to ~17 seconds for promotion. If the consolidation interval\n"
        "is longer, increase post_path_delay_s or trigger manually."
    ),
    post_path_assertions=(
        neo4j_entity("DataForge"),
        neo4j_entity("Apache Flink"),
        neo4j_entity("ClickHouse"),
        neo4j_entity("Priya Sharma"),
        neo4j_promoted("DataForge"),
    ),
    post_path_delay_s=5.0,
)

CP_27 = ConversationPath(
    path_id="CP-27",
    name="Memory-Informed Context Assembly",
    category="Memory Quality",
    objective=(
        "Verify that when Seshat has relevant memory from prior turns, "
        "it appears in assembled context and the memory_enrichment_completed "
        "event shows entities discovered"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "I'm working on a machine learning pipeline called "
                "SentinelML that uses PyTorch for model training and "
                "MLflow for experiment tracking."
            ),
            expected_behavior="Seeds entities: SentinelML, PyTorch, MLflow.",
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message=(
                "SentinelML runs on Kubernetes with GPU node pools. "
                "The inference endpoint uses TorchServe behind an "
                "Istio service mesh."
            ),
            expected_behavior=("More context: Kubernetes, GPU, TorchServe, Istio."),
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message=(
                "What infrastructure changes would you recommend for "
                "scaling SentinelML to handle 10x the current inference load?"
            ),
            expected_behavior=(
                "Analysis request. Should use memory context about "
                "the existing stack (PyTorch, TorchServe, K8s, Istio) "
                "to give a specific — not generic — scaling recommendation."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "strategy", "single"),
                present("memory_enrichment_completed"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 3 response explicitly references SentinelML by name",
        "Recommends scaling TorchServe specifically (not generic model serving)",
        "Addresses Kubernetes GPU node pool scaling",
        "Mentions Istio service mesh considerations for load balancing",
        "Advice is stack-specific, not generic cloud scaling advice",
        "Response demonstrates memory-informed reasoning, not generic knowledge",
    ),
)

CP_28 = ConversationPath(
    path_id="CP-28",
    name="Context Budget Trimming Audit",
    category="Memory Quality",
    objective=(
        "Verify that when context budget is exceeded, trimming decisions "
        "are logged with specific overflow_action and foundational facts "
        "are preserved in the response"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Our production system uses PostgreSQL 16 as the primary "
                "database with pgvector for embeddings."
            ),
            expected_behavior="Establishes foundational architectural fact.",
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message=(
                "We chose PostgreSQL specifically because we needed ACID "
                "guarantees for our financial transaction processing."
            ),
            expected_behavior="Reinforces importance of PostgreSQL (financial context).",
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message=("The API layer is FastAPI with Pydantic v2 for validation."),
            expected_behavior="More context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message="We use Redis for session caching and rate limiting.",
            expected_behavior="More context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "Our observability stack is Prometheus plus Grafana "
                "with OpenTelemetry instrumentation."
            ),
            expected_behavior="More context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "We deploy using ArgoCD with Kustomize overlays "
                "across three environments: dev, staging, production."
            ),
            expected_behavior="More context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "The CI pipeline uses GitHub Actions with matrix builds for Python 3.11 and 3.12."
            ),
            expected_behavior="More context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "We also have a Celery worker fleet for async job processing backed by RabbitMQ."
            ),
            expected_behavior="More context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "Run a full system health check, then tell me about "
                "any issues, and also check the recent error log."
            ),
            expected_behavior=(
                "Tool-heavy turn that adds to context pressure. "
                "Multiple tool calls generate large outputs."
            ),
            assertions=(present("tool_call_completed"),),
        ),
        ConversationTurn(
            user_message=(
                "Given everything we've discussed about our stack, "
                "what is our primary database and why did we choose it?"
            ),
            expected_behavior=(
                "Should still recall PostgreSQL 16 and ACID/financial "
                "context despite potential trimming. Check budget_trimmed "
                "field in gateway_output."
            ),
            assertions=(fld("intent_classified", "task_type", "memory_recall"),),
        ),
    ),
    quality_criteria=(
        "Turn 10 correctly identifies PostgreSQL 16 as primary database",
        "Turn 10 mentions ACID guarantees or financial transaction context",
        "If context was trimmed, foundational facts (PostgreSQL, financial) survived",
        "gateway_output.budget_trimmed field accurately reflects trimming decision",
        "If overflow_action is 'dropped_oldest_history', recent tool output is preserved",
        "If overflow_action is 'dropped_memory_context', session history is preserved",
    ),
)

CP_29 = ConversationPath(
    path_id="CP-29",
    name="Delegation Package Completeness",
    category="Memory Quality",
    objective=(
        "Verify that delegation packages contain sufficient context, "
        "memory excerpts, acceptance criteria, and known pitfalls — "
        "not just correct classification"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Our API uses FastAPI with SQLAlchemy 2.0 async sessions "
                "and Alembic for migrations. The models are in "
                "src/models/ and the routes in src/routes/."
            ),
            expected_behavior="Seeds project context for delegation enrichment.",
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message=(
                "We had a bug last week where a migration dropped a column "
                "that was still referenced by an API endpoint. The tests "
                "didn't catch it because we were mocking the database."
            ),
            expected_behavior="Seeds a known pitfall for delegation context.",
            assertions=(fld("intent_classified", "task_type", "conversational"),),
        ),
        ConversationTurn(
            user_message=(
                "Use Claude Code to add a new REST endpoint for bulk user "
                "imports with CSV upload support, input validation, and "
                "proper error reporting for malformed rows."
            ),
            expected_behavior=(
                "Classifies as DELEGATION. The delegation package should "
                "include: (1) context about FastAPI + SQLAlchemy stack, "
                "(2) memory of the migration bug as a known pitfall, "
                "(3) acceptance criteria covering CSV parsing, validation, "
                "and error reporting, (4) relevant file paths."
            ),
            assertions=(
                fld("intent_classified", "task_type", "delegation"),
                fld("decomposition_assessed", "strategy", "delegate"),
                present("delegation_package_created"),
                gte("delegation_package_created", "criteria_count", 1),
                gte("delegation_package_created", "context_items", 0),
            ),
        ),
    ),
    quality_criteria=(
        "Delegation package references FastAPI + SQLAlchemy from Turn 1",
        "Package includes the migration bug from Turn 2 as a known pitfall",
        "Acceptance criteria cover CSV parsing, validation, and error reporting",
        "Package includes relevant file paths (src/models/, src/routes/)",
        "Task description is self-contained for an agent with no prior context",
        "Package complexity estimate is reasonable (MODERATE or COMPLEX)",
    ),
)

# ============================================================================
# Registry: all paths for easy import
# ============================================================================

ALL_PATHS: tuple[ConversationPath, ...] = (
    CP_01,
    CP_02,
    CP_03,
    CP_04,
    CP_05,
    CP_06,
    CP_07,
    CP_08,
    CP_09,
    CP_10,
    CP_11,
    CP_12,
    CP_13,
    CP_14,
    CP_15,
    CP_16,
    CP_17,
    CP_18,
    CP_19,
    CP_19_V2,
    CP_19_V3,
    CP_19_V4,
    CP_19_V5,
    CP_19_V6,
    CP_19_V7,
    CP_20,
    CP_21,
    CP_22,
    CP_23,
    CP_24,
    CP_25,
    CP_26,
    CP_27,
    CP_28,
    CP_29,
)

PATHS_BY_ID: dict[str, ConversationPath] = {p.path_id: p for p in ALL_PATHS}

PATHS_BY_CATEGORY: dict[str, tuple[ConversationPath, ...]] = {}
for _p in ALL_PATHS:
    _cat = _p.category
    if _cat not in PATHS_BY_CATEGORY:
        PATHS_BY_CATEGORY[_cat] = ()
    PATHS_BY_CATEGORY[_cat] = (*PATHS_BY_CATEGORY[_cat], _p)
