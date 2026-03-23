"""All 25 evaluation conversation paths as Python data structures.

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
"""

from __future__ import annotations

from tests.evaluation.harness.models import (
    ConversationPath,
    ConversationTurn,
    absent,
    fld,
    gte,
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
            expected_behavior=(
                "Responds conversationally. No tool calls. No sub-agents."
            ),
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
            user_message=(
                "Tell me something interesting you've learned recently."
            ),
            expected_behavior=(
                "Continues conversational tone. May draw on general "
                "knowledge. No tool calls."
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
        "Verify that 'have we discussed' triggers MEMORY_RECALL "
        "classification and broad recall"
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
            user_message=(
                "What have we discussed in our conversations so far?"
            ),
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
            expected_behavior=(
                "Provides a recommendation grounded in the prior analysis."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
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
            user_message=(
                "Plan the next steps for adding user authentication "
                "to our API service."
            ),
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
            user_message=(
                "What should we tackle first, and what can we defer?"
            ),
            expected_behavior=(
                "Prioritizes the steps with reasoning."
            ),
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
            expected_behavior=(
                "Follow-up delegation. Enriches the task with test "
                "requirements."
            ),
            assertions=(
                fld("intent_classified", "task_type", "delegation"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "What context would you include in the handoff to make "
                "sure Claude Code doesn't need to ask follow-up questions?"
            ),
            expected_behavior=(
                "Explains DelegationPackage contents: relevant_files, "
                "conventions, known_pitfalls."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
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
        "Verify that self-referential improvement questions trigger "
        "SELF_IMPROVE classification"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "What improvements would you suggest to your own "
                "memory and recall system?"
            ),
            expected_behavior=(
                "Discusses potential improvements to its own architecture."
            ),
            assertions=(
                fld("intent_classified", "task_type", "self_improve"),
                fld("intent_classified", "confidence", 0.85),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Which of those would have the biggest impact on "
                "your usefulness to me?"
            ),
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
    objective=(
        "Verify that explicit tool-use language triggers TOOL_USE "
        "classification"
    ),
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
            user_message=(
                "Read the system log and tell me if anything "
                "looks concerning."
            ),
            expected_behavior=(
                "Calls self_telemetry_query or reads log output. "
                "Reports findings."
            ),
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
