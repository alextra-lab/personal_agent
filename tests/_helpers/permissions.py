"""Install a fixed permission rule set for a test (FRE-1574, ADR-0156 D8).

A call that matches no rule asks, and a test without an approval surface then denies it. A
test that drives a fake tool through ``ToolExecutionLayer`` must therefore say which rules
apply. This helper replaces the rules loader for the test's duration.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from personal_agent.governance import permissions
from personal_agent.governance.permissions import PermissionRules, parse_rule


def use_rules(
    monkeypatch: pytest.MonkeyPatch,
    *,
    allow: Sequence[str] = (),
    ask: Sequence[str] = (),
    deny: Sequence[str] = (),
) -> PermissionRules:
    """Make every permission evaluation in this test use these rules.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        allow: Allow rule texts.
        ask: Ask rule texts.
        deny: Deny rule texts (each gets the reason ``"test deny rule"``).

    Returns:
        The installed rules.
    """
    rules = PermissionRules(
        deny=tuple(parse_rule(text, reason="test deny rule") for text in deny),
        ask=tuple(parse_rule(text, reason="test ask rule") for text in ask),
        allow=tuple(parse_rule(text) for text in allow),
    )
    monkeypatch.setattr(permissions, "load_permission_rules", lambda paths=None: rules)
    return rules
