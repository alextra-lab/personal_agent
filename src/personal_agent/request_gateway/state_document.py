"""Structured context assembly — living state document.

Generates a concise session state summary prepended to each turn's context.
Extracts goals, constraints, open questions, and recent actions from the
conversation history to help the LLM maintain coherent multi-turn reasoning.

Only activated when the session has 3+ turns (no value for fresh conversations).
Target budget: 200-400 tokens.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_MIN_TURNS_FOR_STATE_DOC = 3

_DECISION_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)(?:let'?s\s+(?:go\s+with|use|pick|choose|stick\s+with))"
    r"|(?:(?:we|I)\s+(?:decided|chose|picked|selected|settled)\s+(?:on|to))"
    r"|(?:going\s+(?:with|to\s+use))"
    r"|(?:the\s+decision\s+is)"
    r"|(?:we(?:'ll|\s+will)\s+use)"
)

_QUESTION_PATTERN: re.Pattern[str] = re.compile(r"\?\s*$", re.MULTILINE)


def build_state_document(
    session_messages: Sequence[dict[str, Any]],
    trace_id: str = "",
) -> str | None:
    """Build a structured state document from session history.

    Returns None if the session is too short to benefit from a state doc.

    Args:
        session_messages: Prior conversation history (OpenAI-style format).
        trace_id: Request trace identifier for telemetry.

    Returns:
        Markdown state document string, or None for short sessions.
    """
    if len(session_messages) < _MIN_TURNS_FOR_STATE_DOC:
        return None

    goal = _extract_goal(session_messages)
    constraints = _extract_constraints(session_messages)
    recent_actions = _extract_recent_actions(session_messages)
    open_questions = _extract_open_questions(session_messages)

    sections: list[str] = ["## Current Session State"]

    if goal:
        sections.append(f"**Goal:** {goal}")

    if constraints:
        items = "; ".join(constraints[:5])
        sections.append(f"**Constraints:** {items}")

    if recent_actions:
        actions = "\n".join(f"- {a}" for a in recent_actions)
        sections.append(f"**Recent Actions:**\n{actions}")

    if open_questions:
        questions = "\n".join(f"- {q}" for q in open_questions)
        sections.append(f"**Open Questions:**\n{questions}")

    if len(sections) <= 1:
        return None

    doc = "\n".join(sections)

    logger.debug(
        "state_document_built",
        sections=len(sections) - 1,
        char_count=len(doc),
        trace_id=trace_id,
    )
    return doc


def _extract_goal(messages: Sequence[dict[str, Any]]) -> str | None:
    """Extract the session goal from the first user message.

    Args:
        messages: Session message history.

    Returns:
        Truncated first user message as the goal, or None.
    """
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                return content.strip()[:200]
    return None


def _extract_constraints(
    messages: Sequence[dict[str, Any]],
) -> list[str]:
    """Extract established decisions/constraints from the conversation.

    Scans for decision language patterns in both user and assistant messages.

    Args:
        messages: Session message history.

    Returns:
        List of constraint/decision snippets (max 5).
    """
    constraints: list[str] = []
    for msg in messages:
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue
        for line in content.split("\n"):
            line = line.strip()
            if _DECISION_PATTERNS.search(line) and len(line) > 10:
                constraints.append(line[:150])
                if len(constraints) >= 5:
                    return constraints
    return constraints


def _extract_recent_actions(
    messages: Sequence[dict[str, Any]],
    max_actions: int = 5,
) -> list[str]:
    """Summarize the last N assistant actions.

    Args:
        messages: Session message history.
        max_actions: Maximum number of recent actions to extract.

    Returns:
        List of action summaries from recent assistant messages.
    """
    actions: list[str] = []
    for msg in reversed(list(messages)):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        first_line = content.strip().split("\n")[0][:150]
        actions.append(first_line)
        if len(actions) >= max_actions:
            break
    actions.reverse()
    return actions


def _extract_open_questions(
    messages: Sequence[dict[str, Any]],
    max_questions: int = 3,
) -> list[str]:
    """Extract unanswered questions from recent user messages.

    Looks at the last few user messages for question marks. Skips questions
    that appear to have been answered by a subsequent assistant message.

    Args:
        messages: Session message history.
        max_questions: Maximum questions to extract.

    Returns:
        List of open question strings.
    """
    msg_list = list(messages)
    questions: list[str] = []

    for i in range(len(msg_list) - 1, -1, -1):
        if msg_list[i].get("role") != "user":
            continue
        content = msg_list[i].get("content", "")
        if not isinstance(content, str):
            continue
        if not _QUESTION_PATTERN.search(content):
            continue

        has_response = i + 1 < len(msg_list) and msg_list[i + 1].get("role") == "assistant"
        if has_response:
            continue

        first_question_line = content.strip().split("\n")[0][:150]
        questions.append(first_question_line)
        if len(questions) >= max_questions:
            break

    questions.reverse()
    return questions
