"""Unit tests for orchestrator prompt constants (FRE-383 regression guard).

Guards the anti-fabrication rule that was added to ``_TOOL_RULES`` as a fix for
the incident in which the agent narrated a Neo4j write and a JSON payload that
never happened. The rule must be present in the shared ``_TOOL_RULES`` block and
must flow into both tool-calling strategy prompts so the constraint is enforced
regardless of the strategy the orchestrator selects.
"""

from __future__ import annotations

from personal_agent.orchestrator.prompts import (
    TOOL_USE_NATIVE_PROMPT,
    TOOL_USE_PROMPT_INJECTED,
    _TOOL_RULES,
)

# Substring that must be present in the anti-fabrication rule (FRE-383).
# We assert on a stable fragment rather than the full sentence so the exact
# wording can be refined without breaking the test.
_ANTI_FAB_FRAGMENT = "Never describe the outcome of a system action"


def test_anti_fabrication_rule_in_tool_rules() -> None:
    """_TOOL_RULES contains the anti-fabrication constraint (FRE-383).

    This is the shared source; both tool-use prompt variants embed it via
    f-string interpolation.
    """
    assert _ANTI_FAB_FRAGMENT in _TOOL_RULES, (
        f"Anti-fabrication rule missing from _TOOL_RULES. "
        f"Expected fragment: {_ANTI_FAB_FRAGMENT!r}"
    )


def test_anti_fabrication_rule_in_native_prompt() -> None:
    """TOOL_USE_NATIVE_PROMPT inherits the anti-fabrication rule from _TOOL_RULES."""
    assert _ANTI_FAB_FRAGMENT in TOOL_USE_NATIVE_PROMPT, (
        "Anti-fabrication rule did not propagate into TOOL_USE_NATIVE_PROMPT. "
        "Ensure _TOOL_RULES is interpolated into both prompt constants."
    )


def test_anti_fabrication_rule_in_injected_prompt() -> None:
    """TOOL_USE_PROMPT_INJECTED inherits the anti-fabrication rule from _TOOL_RULES."""
    assert _ANTI_FAB_FRAGMENT in TOOL_USE_PROMPT_INJECTED, (
        "Anti-fabrication rule did not propagate into TOOL_USE_PROMPT_INJECTED. "
        "Ensure _TOOL_RULES is interpolated into both prompt constants."
    )


def test_no_invent_tools_rule_unchanged() -> None:
    """The adjacent 'Do not invent tools' rule must still be present.

    Regression guard: verify the FRE-383 edit did not accidentally remove or
    truncate the existing tool-invention constraint.
    """
    assert "Do not invent tools or parameters" in _TOOL_RULES
