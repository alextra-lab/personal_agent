"""The "allow for this session" rules (ADR-0156 D8.3).

When the user answers a card with "allow for this session", a session allow rule is added for
the exact tool and arguments of that call: ``tool(=<canonical JSON>)``. The same call then runs
without a card for the rest of the session. The card offers this answer only for a call that
matched no rule, so a session rule never stands in front of an explicit ask rule. The evaluation
order enforces the same: ask rules are checked before session rules.

The rules live in this process only. A gateway restart clears them.
"""

from __future__ import annotations

from collections.abc import Mapping

from personal_agent.governance.permissions import (
    PermissionRule,
    canonical_arguments,
    parse_rule,
)

_SESSION_RULES: dict[str, dict[str, PermissionRule]] = {}


def add_session_rule(session_id: str, tool: str, arguments: Mapping[str, object]) -> str:
    """Add an allow rule for one exact call, for the rest of the session.

    Args:
        session_id: The session the rule belongs to.
        tool: The tool name.
        arguments: The call's arguments, exactly as shown on the card.

    Returns:
        The rule text.

    Raises:
        TypeError: When an argument is not JSON-serialisable.
    """
    text = f"{tool}(={canonical_arguments(arguments)})"
    rule = parse_rule(text, allow_session_form=True)
    _SESSION_RULES.setdefault(session_id, {})[rule.text] = rule
    return rule.text


def session_rules(session_id: str | None) -> tuple[PermissionRule, ...]:
    """Return a session's allow rules.

    Args:
        session_id: The session, or ``None`` outside a session.

    Returns:
        The rules, empty when the session has none.
    """
    if not session_id:
        return ()
    return tuple(_SESSION_RULES.get(session_id, {}).values())


def clear_session_rules(session_id: str | None = None) -> None:
    """Remove one session's rules, or every session's rules.

    Args:
        session_id: The session to clear, or ``None`` for all.
    """
    if session_id is None:
        _SESSION_RULES.clear()
    else:
        _SESSION_RULES.pop(session_id, None)
