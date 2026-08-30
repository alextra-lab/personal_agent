"""The injected taxonomy + probe prompt (AC-2: verbatim, uncontaminated).

Definitions below are honest paraphrases of the pattern-bank comments in
``personal_agent.request_gateway.intent`` — the same seven ``TaskType`` members the
deterministic stage sorts into, described in prose rather than regex. Nothing here reads
the deterministic classifier's own answer for any given message; ``build_probe_prompt``
is a pure function of the user message alone.
"""

from __future__ import annotations

from personal_agent.request_gateway.types import TaskType

TASK_TYPE_DEFINITIONS: dict[TaskType, str] = {
    TaskType.CONVERSATIONAL: (
        "General chat, greetings, or questions with no more specific match below — the "
        "default when nothing else applies."
    ),
    TaskType.MEMORY_RECALL: (
        "Asking the agent to recall something from a prior conversation or session "
        "('do you remember...', 'what did we decide about...', 'last time we talked...')."
    ),
    TaskType.ANALYSIS: (
        "Deep analysis, research, investigation, evaluation, or comparison — weighing "
        "trade-offs, pros and cons, or recommendations that require gathering and "
        "synthesizing information rather than answering from what's already known."
    ),
    TaskType.PLANNING: (
        "Creating a plan, roadmap, timeline, or breaking a project or task down into "
        "phases or steps."
    ),
    TaskType.DELEGATION: (
        "Coding work — writing, debugging, refactoring, or testing code, or reviewing a "
        "pull request — the kind of task handed to an external coding agent."
    ),
    TaskType.SELF_IMPROVE: (
        "The agent reasoning about or proposing changes to its own architecture, memory, "
        "routing, or Captain's Log."
    ),
    TaskType.TOOL_USE: (
        "A request that needs a specific tool invoked — searching, reading a file, "
        "running a command, checking logs or system health, or building an artifact "
        "(dashboard, chart, web page)."
    ),
}

_TAXONOMY_LINES = "\n".join(
    f"- {member.value}: {definition}" for member, definition in TASK_TYPE_DEFINITIONS.items()
)

PROBE_SYSTEM_PROMPT = f"""\
You are classifying a single user message into exactly one task type from this taxonomy:

{_TAXONOMY_LINES}

Read only the message below — you have no other context about it: no conversation \
history, no tool results, no prior turns. Classify it as it would look at the very start \
of a brand new conversation.

Respond with JSON only, no markdown fences, no other text:
{{"task_type": "<one of: {", ".join(m.value for m in TASK_TYPE_DEFINITIONS)}>", \
"reason": "<one sentence: why this type, not the others>"}}"""


def build_probe_prompt(user_message: str) -> str:
    """Build the verbatim user-turn text for the classification probe (AC-2).

    Pure function of ``user_message`` alone — the taxonomy is fixed (see
    :data:`PROBE_SYSTEM_PROMPT`), and nothing about the deterministic classifier's own
    answer for this message is read or referenced.

    Args:
        user_message: The raw user message to classify — the exact text Stage 4 of the
            gateway would have seen.

    Returns:
        The user-turn content to send alongside :data:`PROBE_SYSTEM_PROMPT`.
    """
    return f"Classify this message:\n\n{user_message}"
