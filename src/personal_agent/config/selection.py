"""Per-turn model selection context (ADR-0121 §4 / FRE-917).

The server-authoritative selection store (session → role → deployment key) is
resolved once at turn launch and carried through the orchestrator call chain via
an async-safe context variable — the same mechanism ``config.profile`` uses for
the execution profile it replaces. Setting it once per background task means
each ``asyncio.Task`` gets its own copy, so a selection change (a ``PATCH`` from
another turn) never mutates a turn already in flight (ADR-0079 invariant 7 /
AC-7): the turn resolves against the snapshot it launched with.

In T2 the map carries only ``{"primary": <key>}`` — the sole standing
user-selectable role. Pinned and per-build roles are not placed here; the
factory consults this map only for a role that carries a selection and falls
through to its existing resolution otherwise.
"""

from __future__ import annotations

import contextvars
from collections.abc import Mapping
from types import MappingProxyType

#: Async-safe map of role → selected deployment key for the current turn. Empty
#: when no selection has been resolved (e.g. a background task outside the chat
#: turn path); the factory then resolves roles through their binding defaults.
#: The default is an immutable empty mapping so it can be safely shared as a
#: ContextVar default (never mutated in place — writers create a fresh dict).
_current_selection: contextvars.ContextVar[Mapping[str, str]] = contextvars.ContextVar(
    "current_selection", default=MappingProxyType({})
)


def set_current_selection(
    selection: Mapping[str, str],
) -> contextvars.Token[Mapping[str, str]]:
    """Set the resolved model selection for the current async context.

    Args:
        selection: Map of role → deployment key resolved for this turn. Values
            are expected to be already guardrail-validated (see
            :func:`personal_agent.config.model_loader.resolve_selected_deployment`)
            — this carrier does not re-validate.

    Returns:
        A token that can be passed to :func:`reset_current_selection` to restore
        the previous value (useful in tests).
    """
    return _current_selection.set(dict(selection))


def get_current_selection(role: str) -> str | None:
    """Return the selected deployment key for ``role`` in the current context.

    Args:
        role: The role to look up.

    Returns:
        The selected deployment key, or ``None`` when no selection is carried
        for this role (the factory then uses the role's default resolution).
    """
    return _current_selection.get().get(role)


def reset_current_selection(token: contextvars.Token[Mapping[str, str]]) -> None:
    """Restore the selection map to a prior value (useful in tests).

    Args:
        token: The token returned by :func:`set_current_selection`.
    """
    _current_selection.reset(token)
