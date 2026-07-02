"""Constraint governance action-ID registry (ADR-0076).

Each governed constraint exposes a fixed set of options. Every option has a
stable ``action_id`` (snake_case) that is independent of the display label
shown in the PWA ``DecisionCard``. Stored preferences and wire messages carry
the ``action_id``, so renaming a button label never invalidates persisted
state.

Convention: the **last** option in each list is the safe default applied on
timeout, disconnect, or no active WebSocket connection.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConstraintOption:
    """A single user-selectable option for a constraint pause.

    Attributes:
        action_id: Stable identifier persisted in preferences and sent on the
            wire. Never changes once shipped.
        label: Human-readable display label rendered by the PWA.
    """

    action_id: str
    label: str


CONSTRAINT_OPTIONS: dict[str, list[ConstraintOption]] = {
    "tool_iteration_limit": [
        ConstraintOption(action_id="continue_10", label="Continue (10 more)"),
        ConstraintOption(action_id="finish_now", label="Finish now"),
    ],
    "context_compression": [
        ConstraintOption(action_id="compress_continue", label="Compress and continue"),
        ConstraintOption(action_id="stop_here", label="Stop here instead"),
    ],
    # ADR-0101 §8b / FRE-691: pre-flight cloud-attachment cost confirmation. The
    # safe default (last) is keep_local — no cloud spend without explicit confirm.
    "attachment_cost": [
        ConstraintOption(action_id="proceed_cloud", label="Proceed on cloud"),
        ConstraintOption(action_id="keep_local", label="Keep local / free"),
    ],
}


def option_ids(constraint: str) -> list[str]:
    """Return the valid ``action_id`` values for a constraint.

    Args:
        constraint: Constraint name (key of :data:`CONSTRAINT_OPTIONS`).

    Returns:
        List of stable ``action_id`` strings, in display order.

    Raises:
        KeyError: If ``constraint`` is not a known constraint name.
    """
    return [opt.action_id for opt in CONSTRAINT_OPTIONS[constraint]]


def default_action_id(constraint: str) -> str:
    """Return the safe default ``action_id`` for a constraint.

    The default is the last option in the constraint's option list — applied
    on timeout, disconnect, or when no WebSocket connection is active.

    Args:
        constraint: Constraint name (key of :data:`CONSTRAINT_OPTIONS`).

    Returns:
        The default option's stable ``action_id``.

    Raises:
        KeyError: If ``constraint`` is not a known constraint name.
    """
    return CONSTRAINT_OPTIONS[constraint][-1].action_id
