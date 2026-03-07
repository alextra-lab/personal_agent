"""Deterministic routing gate and role resolution for single-model mode.

Pre-router heuristics avoid LLM calls when confidence is high.
resolve_role() maps requested roles to actual runtime roles (e.g. REASONING -> STANDARD when disabled).
"""

import re

from personal_agent.config import settings
from personal_agent.llm_client import ModelRole
from personal_agent.orchestrator.types import HeuristicRoutingPlan

# CODING: code fences, stack traces, def/class/import, debug/refactor/implement, diffs, CI
# Use "from\s+\S+\s+import" so natural language ("fly from X to Y") is not matched.
_CODING_PATTERNS = re.compile(
    r"(?:^|\s)(?:def\s|class\s|import\s|from\s+\S+\s+import\s|```[\s\S]*?```|"
    r"debug|refactor|implement|fix\s+(?:the\s+)?(?:bug|code)|"
    r"stack\s+trace|traceback|File\s+\".*\"|AssertionError|TypeError|"
    r"diff\s|patch\s|\.patch\b|CI\s+(?:failed|error)|build\s+failed)",
    re.IGNORECASE,
)
_CODING_KEYWORDS = (
    "code review", "unit test", "write a function", "write a class",
    "implement ", "refactor ", "debug ", "bug ", "syntax error", "lint ",
)

# STANDARD: explicit tool intent
_TOOL_INTENT_PATTERNS = re.compile(
    r"(?:search\s+(?:the\s+)?web|look\s+up|list\s+files|read\s+file|"
    r"check\s+disk\s+usage|open\s+url|latest\s+news|"
    r"search\s+internet|web\s+search|find\s+(?:on\s+)?(?:the\s+)?web)",
    re.IGNORECASE,
)

# REASONING: prove/derive/rigorously, deep reasoning, research synthesis
_REASONING_PATTERNS = re.compile(
    r"(?:prove|derive|rigorously|deep\s+reasoning|research\s+synthesis|"
    r"multi-step\s+(?:formal\s+)?analysis|step-by-step\s+proof|"
    r"formal\s+analysis|careful\s+reasoning)",
    re.IGNORECASE,
)

# MEMORY RECALL: questions about the user's own history (ADR-0025)
_MEMORY_RECALL_PATTERNS = re.compile(
    r"(?:"
    r"what\s+(?:have\s+I|did\s+I|topics?\s+have\s+I|things?\s+have\s+I)|"
    r"have\s+I\s+(?:ever|asked|mentioned|talked|discussed)|"
    r"did\s+I\s+(?:ask|mention|talk|discuss)|"
    r"do\s+you\s+remember|"
    r"(?:my|our)\s+(?:past|previous|earlier|last)\s+(?:question|conversation|session|discussion)|"
    r"last\s+time\s+(?:I|we)\s+(?:asked|talked|discussed)|"
    r"remind\s+me\s+(?:what|about)|"
    r"what\s+(?:else\s+)?(?:have\s+we|have\s+I)\s+(?:talked|discussed|covered)"
    r")",
    re.IGNORECASE,
)


def is_memory_recall_query(user_message: str) -> bool:
    """Return True if the user is asking about their own history.

    Used by step_init to select the broad-recall memory query path (ADR-0025).

    Args:
        user_message: Raw user input.

    Returns:
        True if message matches a memory-recall intent pattern.
    """
    return bool(_MEMORY_RECALL_PATTERNS.search(user_message or ""))


def heuristic_routing(user_message: str) -> HeuristicRoutingPlan:
    """Run deterministic classifier on user message (no LLM).

    Returns:
        HeuristicRoutingPlan with target_model, confidence, reason, used_heuristics=True.
    """
    text = (user_message or "").strip()
    if not text:
        return {
            "target_model": ModelRole.STANDARD,
            "confidence": 0.9,
            "reason": "Empty message, default to STANDARD",
            "used_heuristics": True,
        }

    # CODING
    if _CODING_PATTERNS.search(text):
        return {
            "target_model": ModelRole.CODING,
            "confidence": 0.9,
            "reason": "Code-related patterns (def/class/import/debug/diff/CI)",
            "used_heuristics": True,
        }
    lower = text.lower()
    if any(k in lower for k in _CODING_KEYWORDS):
        return {
            "target_model": ModelRole.CODING,
            "confidence": 0.85,
            "reason": "Coding keywords detected",
            "used_heuristics": True,
        }

    # STANDARD: explicit tool intent
    if _TOOL_INTENT_PATTERNS.search(text):
        return {
            "target_model": ModelRole.STANDARD,
            "confidence": 0.9,
            "reason": "Explicit tool intent (search/list/read/open)",
            "used_heuristics": True,
        }

    # REASONING
    if _REASONING_PATTERNS.search(text):
        return {
            "target_model": ModelRole.REASONING,
            "confidence": 0.85,
            "reason": "Deep reasoning / proof / research requested",
            "used_heuristics": True,
        }

    # Default
    return {
        "target_model": ModelRole.STANDARD,
        "confidence": 0.7,
        "reason": "Default to STANDARD",
        "used_heuristics": True,
    }


def resolve_role(requested_role: ModelRole) -> ModelRole:
    """Map requested model role to actual runtime role (single-model mode).

    - If router_role is STANDARD, ROUTER -> STANDARD.
    - If enable_reasoning_role is False, REASONING -> STANDARD.
    - CODING stays CODING (dedicated specialist).
    """
    role_upper = requested_role.value.upper()
    if role_upper == "ROUTER":
        router_cfg = (getattr(settings, "router_role", None) or "ROUTER").upper()
        if router_cfg == "STANDARD":
            return ModelRole.STANDARD
        return ModelRole.ROUTER
    if role_upper == "REASONING" and not getattr(settings, "enable_reasoning_role", True):
        return ModelRole.STANDARD
    return requested_role
