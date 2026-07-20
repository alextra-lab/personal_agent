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
        "attachment_unsupported",
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
    the generic fallback. The generic branch delegates the reason string to
    :func:`~personal_agent.security.sanitize_error_message` so that sensitive
    details are stripped.

    Placement-neutral (ADR-0121 T5, FRE-920): the copy no longer distinguishes
    local vs. cloud, and there is no "switch to cloud" action — Path is
    removed, so retrying means retrying, not escalating to a different path.

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

    retry_actions = ("retry", "stop")

    if isinstance(error, LLMServerError):
        return ClassifiedError(
            category="model_server",
            reason="The model server returned an error (it may have timed out on a large request).",
            next_step="Retry or shorten the request.",
            actions=retry_actions,
        )

    if isinstance(error, LLMTimeout):
        return ClassifiedError(
            category="timeout",
            reason="The model timed out — the request was large.",
            next_step="Retry or shorten it.",
            actions=retry_actions,
        )

    # InferenceSlotTimeout (concurrency.py) — slot contention is a timeout variant.
    try:
        from personal_agent.llm_client.concurrency import InferenceSlotTimeout

        if isinstance(error, InferenceSlotTimeout):
            return ClassifiedError(
                category="timeout",
                reason="The model server was busy and the request timed out waiting for a slot.",
                next_step="Retry in a moment.",
                actions=retry_actions,
            )
    except ImportError:
        pass

    if isinstance(error, LLMConnectionError):
        return ClassifiedError(
            category="connection",
            reason="Couldn't reach the model server.",
            next_step="Retry in a moment.",
            actions=retry_actions,
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

    from personal_agent.exceptions import AttachmentUnsupportedError

    if isinstance(error, AttachmentUnsupportedError):
        return ClassifiedError(
            category="attachment_unsupported",
            reason=str(error),
            next_step="Remove the attachment and resubmit.",
            actions=("stop",),
        )

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
