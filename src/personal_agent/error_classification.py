"""Structured error classification for turn failures (FRE-398).

Maps exception types to a :class:`ClassifiedError` with a human-readable
reason, concrete next-step guidance, and stable action ids for PWA buttons.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal


@dataclass(frozen=True)
class ClassifiedError:
    """Structured description of a turn failure.

    Attributes:
        category: Machine-readable failure class.
        reason: Human-readable explanation of what happened (no internals).
        next_step: Concrete guidance for what the user can do next.
        actions: Stable action ids for PWA action buttons (e.g. ``retry``).
        partial: ``True`` when a partial reply was salvaged from gathered work.
    """

    category: Literal[
        "model_server",
        "timeout",
        "connection",
        "rate_limit",
        "budget_denied",
        "tool_failure",
        "generic",
    ]
    reason: str
    next_step: str
    actions: tuple[str, ...]
    partial: bool = False


def classify_error(error: Exception) -> ClassifiedError:
    """Classify an exception into a :class:`ClassifiedError`.

    Uses ``isinstance`` checks in priority order so subclasses of
    :class:`~personal_agent.llm_client.types.LLMClientError` are caught before
    the generic fallback.  The generic branch delegates the reason string to
    :func:`~personal_agent.security.sanitize_error_message` so that sensitive
    details are stripped.

    Args:
        error: The exception that caused the turn to fail.

    Returns:
        A :class:`ClassifiedError` with category, reason, next_step, and
        stable action ids.
    """
    # Import locally to avoid circular-import risk (these are leaf modules).
    from personal_agent.llm_client.types import (
        LLMConnectionError,
        LLMRateLimit,
        LLMServerError,
        LLMTimeout,
    )

    if isinstance(error, LLMServerError):
        return ClassifiedError(
            category="model_server",
            reason="The local model server hit an error (it may have timed out on a large request).",
            next_step="Retry, switch to Cloud, or shorten the request.",
            actions=("retry", "switch_to_cloud", "stop"),
        )

    if isinstance(error, LLMTimeout):
        return ClassifiedError(
            category="timeout",
            reason="The local model timed out — the request was large.",
            next_step="Retry, switch to Cloud, or shorten it.",
            actions=("retry", "switch_to_cloud", "stop"),
        )

    # InferenceSlotTimeout (concurrency.py) — slot contention is a timeout variant.
    try:
        from personal_agent.llm_client.concurrency import InferenceSlotTimeout

        if isinstance(error, InferenceSlotTimeout):
            return ClassifiedError(
                category="timeout",
                reason="The model server was busy and the request timed out waiting for a slot.",
                next_step="Retry in a moment or switch to Cloud.",
                actions=("retry", "switch_to_cloud", "stop"),
            )
    except ImportError:
        pass

    if isinstance(error, LLMConnectionError):
        return ClassifiedError(
            category="connection",
            reason="Couldn't reach the local model server.",
            next_step="Check the SLM server is running, then retry or switch to Cloud.",
            actions=("retry", "switch_to_cloud", "stop"),
        )

    if isinstance(error, LLMRateLimit):
        return ClassifiedError(
            category="rate_limit",
            reason="The model server is rate-limiting requests right now.",
            next_step="Wait a moment, then retry.",
            actions=("retry", "stop"),
        )

    # BudgetDenied carries structured payload — use it for the reason.
    try:
        from personal_agent.cost_gate.types import BudgetDenied

        if isinstance(error, BudgetDenied):
            reason = (
                f"Budget cap reached for role '{error.role}' "
                f"({error.time_window} window, "
                f"spent ${error.current_spend:.2f} of ${error.cap:.2f})."
            )
            return ClassifiedError(
                category="budget_denied",
                reason=reason,
                next_step="Raise the budget for this window or wait for it to reset.",
                actions=("stop",),
            )
    except ImportError:
        pass

    # Generic fallback — delegate reason to sanitize_error_message.
    from personal_agent.security import sanitize_error_message

    return ClassifiedError(
        category="generic",
        reason=sanitize_error_message(error),
        next_step="Try rephrasing or retry.",
        actions=("retry", "stop"),
    )


def with_partial(classified: ClassifiedError) -> ClassifiedError:
    """Return a copy of *classified* with ``partial=True``.

    Args:
        classified: The original :class:`ClassifiedError`.

    Returns:
        A new frozen instance with ``partial`` set to ``True``.
    """
    return replace(classified, partial=True)
