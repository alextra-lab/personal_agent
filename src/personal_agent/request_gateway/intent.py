"""Stage 4: Intent Classification.

Deterministic (regex + heuristics) classification of user messages
into task types. Evolved from orchestrator/routing.py heuristic_routing().

The LLM is NOT used for classification -- that is the entire point.
Classification drives context assembly, not model selection.
"""

from __future__ import annotations

import re

from personal_agent.request_gateway.types import (
    Complexity,
    IntentResult,
    TaskType,
)

# ---------------------------------------------------------------------------
# Pattern banks -- evolved from orchestrator/routing.py
# ---------------------------------------------------------------------------

_MEMORY_RECALL_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)"
    r"(?:what\s+(?:have\s+I|did\s+I|topics?\s+have\s+(?:I|we)|things?\s+have\s+I)\s+)"
    r"|(?:do\s+you\s+remember)"
    r"|(?:last\s+time\s+(?:we|I)\s+(?:asked|talked|discussed))"
    r"|(?:have\s+(?:we|I)\s+(?:discussed|talked|spoken))"
    r"|(?:what\s+(?:do\s+you\s+know|did\s+(?:I|we))\s+)"
    r"|(?:recall\s+(?:our|my|the)\s+)"
    r"|(?:what\s+(?:have\s+)?(?:I|we)\s+(?:decided|concluded|said))",
)

_CODING_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)"
    r"(?:```)"
    r"|(?:(?:def|class|import|from)\s+\w+)"
    r"|(?:(?:debug|refactor|implement|fix|write|add)\s+(?:the\s+|this\s+|a\s+|an\s+|my\s+|new\s+)?"
    r"(?:code|function|class|module|test|endpoint|route|api|bug|CI|pipeline|failure))"
    r"|(?:traceback|stack\s*trace|error\s*log)"
    r"|(?:(?:unit|integration)\s*test)"
    r"|(?:pull\s*request|PR\s+review|code\s+review)"
    r"|(?:use\s+(?:claude\s+code|codex|copilot|cursor)\s+to\s+)",
)

_CODING_KEYWORDS: tuple[str, ...] = (
    "write a function",
    "code review",
    "unit test",
    "integration test",
    "fix the bug",
    "implement the",
    "add an endpoint",
    "add a new endpoint",
    "use claude code",
    "refactor",
)

_ANALYSIS_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)"
    r"(?:(?:analyze|analyse|research|investigate|evaluate|compare)\s+)"
    r"|(?:think\s+step[\s-]*by[\s-]*step)"
    r"|(?:(?:deep|thorough|rigorous|careful)\s+(?:analysis|thinking|review))"
    r"|(?:trade[\s-]*offs?\s+(?:between|of))"
    r"|(?:(?:pros?\s+and\s+cons?|advantages?\s+and\s+disadvantages?))"
    r"|(?:recommend(?:ation)?s?\s+(?:for|on|about))",
)

_TOOL_INTENT_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)"
    r"(?:(?:search|find|look\s*up|grep|glob)\s+(?:for\s+)?)"
    r"|(?:(?:list|show|display)\s+(?:the\s+)?(?:tools?|files?|endpoints?))"
    r"|(?:(?:read|open|view)\s+(?:\w+\s+)*?(?:file|config|log|url|browser))"
    r"|(?:run\s+(?:the\s+)?(?:command|script|test))",
)

_SELF_IMPROVE_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)"
    r"(?:(?:improve|optimize|enhance|change)\s+(?:your|the|my)\s+"
    r"(?:own|architecture|system|memory|routing|performance))"
    r"|(?:captain'?s?\s+log)"
    r"|(?:(?:your|the\s+agent'?s?)\s+(?:proposals?|improvements?|suggestions?))"
    r"|(?:what\s+(?:changes|improvements)\s+(?:would\s+you|have\s+you|do\s+you))"
    r"|(?:(?:propose|suggest)\s+(?:changes?\s+)?to\s+(?:your|the)\s+(?:own\s+)?(?:architecture|system))"
    r"|(?:modify\s+(?:yourself|your\s+own))",
)

_PLANNING_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)"
    r"(?:(?:plan|outline|roadmap|break\s*down|decompose)\s+)"
    r"|(?:(?:create|write|draft)\s+(?:a\s+)?(?:plan|roadmap|timeline|schedule))"
    r"|(?:(?:next|upcoming)\s+(?:sprint|iteration|phase|steps?))"
    r"|(?:(?:implementation|project)\s+(?:plan|steps|phases?))"
    r"|(?:break\s+(?:this|it)\s+(?:\w+\s+)*?into\s+(?:tasks|steps|pieces))",
)

# ---------------------------------------------------------------------------
# Multi-step action verb detection for complexity estimation
# ---------------------------------------------------------------------------

_ACTION_VERBS: re.Pattern[str] = re.compile(
    r"(?i)\b(?:research|analyze|analyse|compare|draft|write|implement|"
    r"evaluate|investigate|recommend|design|plan|outline|create|build|"
    r"review|assess|benchmark|test|deploy|refactor|optimize)\b",
)

# ---------------------------------------------------------------------------
# Complexity estimation
# ---------------------------------------------------------------------------

_QUESTION_MARK_RE: re.Pattern[str] = re.compile(r"\?")
_SENTENCE_RE: re.Pattern[str] = re.compile(r"[.!?]+")


def _estimate_complexity(message: str, task_type: TaskType) -> Complexity:
    """Estimate task complexity from message properties.

    Heuristics considered:
    - Word count (short = simple, long = moderate/complex)
    - Question count (multiple questions = higher complexity)
    - Sentence count (multi-sentence = moderate+)
    - Action verb count (multiple distinct actions = complex)
    - Task type bias (analysis/planning inherently more complex)

    Args:
        message: The user's message text.
        task_type: Already-classified task type (some types bias complexity).

    Returns:
        Estimated complexity level.
    """
    word_count = len(message.split())
    question_count = len(_QUESTION_MARK_RE.findall(message))
    sentence_count = max(1, len(_SENTENCE_RE.split(message)))
    action_verb_count = len(_ACTION_VERBS.findall(message))

    # Short simple messages
    if word_count < 15 and question_count <= 1 and action_verb_count <= 1:
        return Complexity.SIMPLE

    # Multiple questions suggest moderate+
    if question_count >= 3:
        return Complexity.COMPLEX

    # Multiple action verbs in analysis/planning = complex multi-step request
    if action_verb_count >= 3 and task_type in (
        TaskType.ANALYSIS,
        TaskType.PLANNING,
        TaskType.DELEGATION,
    ):
        return Complexity.COMPLEX

    # Long messages with analysis intent
    if word_count > 100 and task_type in (TaskType.ANALYSIS, TaskType.PLANNING):
        return Complexity.COMPLEX

    # Medium messages or multi-sentence
    if word_count > 40 or sentence_count > 3 or question_count >= 2:
        return Complexity.MODERATE

    # Multi-verb tasks that did not hit the >= 3 threshold
    if action_verb_count >= 2:
        return Complexity.MODERATE

    return Complexity.SIMPLE


# ---------------------------------------------------------------------------
# Main classification
# ---------------------------------------------------------------------------


def classify_intent(user_message: str) -> IntentResult:
    """Classify user message into a task type with complexity estimate.

    Deterministic classification using regex patterns and heuristics.
    No LLM call. This is Stage 4 of the gateway pipeline.

    Priority order (first match wins):
    1. Memory recall (highest priority -- specific intent)
    2. Self-improvement (agent self-referential)
    3. Coding (maps to DELEGATION -- coding delegates externally)
    4. Planning
    5. Analysis/reasoning
    6. Tool use
    7. Conversational (default)

    Args:
        user_message: The raw user message text.

    Returns:
        IntentResult with task type, complexity, confidence, and signals.
    """
    signals: list[str] = []

    # 1. Memory recall
    if _MEMORY_RECALL_PATTERNS.search(user_message):
        signals.append("memory_recall_pattern")
        task_type = TaskType.MEMORY_RECALL
        confidence = 0.9
        complexity = _estimate_complexity(user_message, task_type)
        return IntentResult(
            task_type=task_type,
            complexity=complexity,
            confidence=confidence,
            signals=signals,
        )

    # 2. Self-improvement
    if _SELF_IMPROVE_PATTERNS.search(user_message):
        signals.append("self_improve_pattern")
        task_type = TaskType.SELF_IMPROVE
        confidence = 0.85
        complexity = _estimate_complexity(user_message, task_type)
        return IntentResult(
            task_type=task_type,
            complexity=complexity,
            confidence=confidence,
            signals=signals,
        )

    # 3. Coding -> DELEGATION
    if _CODING_PATTERNS.search(user_message) or any(
        kw in user_message.lower() for kw in _CODING_KEYWORDS
    ):
        signals.append("coding_pattern")
        task_type = TaskType.DELEGATION
        confidence = 0.85
        complexity = _estimate_complexity(user_message, task_type)
        return IntentResult(
            task_type=task_type,
            complexity=complexity,
            confidence=confidence,
            signals=signals,
        )

    # 4. Planning
    if _PLANNING_PATTERNS.search(user_message):
        signals.append("planning_pattern")
        task_type = TaskType.PLANNING
        confidence = 0.8
        complexity = _estimate_complexity(user_message, task_type)
        return IntentResult(
            task_type=task_type,
            complexity=complexity,
            confidence=confidence,
            signals=signals,
        )

    # 5. Analysis / reasoning
    if _ANALYSIS_PATTERNS.search(user_message):
        signals.append("analysis_pattern")
        task_type = TaskType.ANALYSIS
        confidence = 0.8
        complexity = _estimate_complexity(user_message, task_type)
        return IntentResult(
            task_type=task_type,
            complexity=complexity,
            confidence=confidence,
            signals=signals,
        )

    # 6. Tool use
    if _TOOL_INTENT_PATTERNS.search(user_message):
        signals.append("tool_intent_pattern")
        task_type = TaskType.TOOL_USE
        confidence = 0.8
        complexity = Complexity.SIMPLE
        return IntentResult(
            task_type=task_type,
            complexity=complexity,
            confidence=confidence,
            signals=signals,
        )

    # 7. Default: conversational
    signals.append("no_special_patterns")
    task_type = TaskType.CONVERSATIONAL
    complexity = _estimate_complexity(user_message, task_type)
    return IntentResult(
        task_type=task_type,
        complexity=complexity,
        confidence=0.7,
        signals=signals,
    )
