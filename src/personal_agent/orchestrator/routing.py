"""Deterministic routing utilities.

heuristic_routing() is retained for observability (classifies message intent)
but the two-tier taxonomy (ADR-0033) means all paths use PRIMARY.
resolve_role() is simplified — no deprecated role aliasing needed.
"""

import re

from personal_agent.llm_client import ModelRole
from personal_agent.orchestrator.types import HeuristicRoutingPlan

# MEMORY RECALL: questions about the user's own history (ADR-0025)
_MEMORY_RECALL_PATTERNS = re.compile(
    r"(?:"
    r"what\s+do\s+you\s+remember(?:\s+about)?|"
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
    """Classify user message intent (observability only — ADR-0033).

    All requests route to PRIMARY in the two-tier taxonomy; this function
    is retained for logging and test purposes.

    Returns:
        HeuristicRoutingPlan with target_model=PRIMARY, confidence, reason, used_heuristics=True.
    """
    text = (user_message or "").strip()
    if not text:
        return {
            "target_model": ModelRole.PRIMARY,
            "confidence": 0.9,
            "reason": "Empty message, default to PRIMARY",
            "used_heuristics": True,
        }

    return {
        "target_model": ModelRole.PRIMARY,
        "confidence": 1.0,
        "reason": "Two-tier taxonomy: all requests use PRIMARY (ADR-0033)",
        "used_heuristics": True,
    }


def resolve_role(requested_role: ModelRole) -> ModelRole:
    """Return the runtime model role (identity mapping in two-tier taxonomy).

    ADR-0033: No deprecated role aliasing — both PRIMARY and SUB_AGENT
    are used as-is.
    """
    return requested_role
