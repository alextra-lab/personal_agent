"""Unit tests for orchestrator prompt constants (FRE-383 regression guard).

Guards the anti-fabrication rule that was added to ``_TOOL_RULES`` as a fix for
the incident in which the agent narrated a Neo4j write and a JSON payload that
never happened. The rule must be present in the shared ``_TOOL_RULES`` block and
must flow into both tool-calling strategy prompts so the constraint is enforced
regardless of the strategy the orchestrator selects.
"""

from __future__ import annotations

from personal_agent.orchestrator.prompts import (
    _TOOL_RULES,
    TOOL_USE_NATIVE_PROMPT,
    TOOL_USE_PROMPT_INJECTED,
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
        f"Anti-fabrication rule missing from _TOOL_RULES. Expected fragment: {_ANTI_FAB_FRAGMENT!r}"
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


# Substring that must be present in the verifiability search trigger (FRE-1278 defect 1).
_VERIFIABILITY_TRIGGER_FRAGMENT = "recommending, comparing, identifying, pricing, or locating"

# Substring that must be present in the grounding rule (FRE-1278 defect 2).
_GROUNDING_RULE_FRAGMENT = "name only brands, products, people, organisations, or shops"


def test_verifiability_trigger_in_tool_rules() -> None:
    """_TOOL_RULES searches before naming brands/products/etc regardless of recency (FRE-1278).

    The prior trigger was keyed only to recency ("current events", "live web data"), so a
    question the model simply doesn't know (e.g. "which tinned tuna brand in France") never
    tripped it. This rule is additive and independent of the recency framing.
    """
    assert _VERIFIABILITY_TRIGGER_FRAGMENT in _TOOL_RULES, (
        f"Verifiability search trigger missing from _TOOL_RULES. "
        f"Expected fragment: {_VERIFIABILITY_TRIGGER_FRAGMENT!r}"
    )


def test_verifiability_trigger_in_native_prompt() -> None:
    """TOOL_USE_NATIVE_PROMPT inherits the verifiability trigger from _TOOL_RULES."""
    assert _VERIFIABILITY_TRIGGER_FRAGMENT in TOOL_USE_NATIVE_PROMPT


def test_verifiability_trigger_in_injected_prompt() -> None:
    """TOOL_USE_PROMPT_INJECTED inherits the verifiability trigger from _TOOL_RULES."""
    assert _VERIFIABILITY_TRIGGER_FRAGMENT in TOOL_USE_PROMPT_INJECTED


def test_grounding_rule_in_tool_rules() -> None:
    """_TOOL_RULES requires named entities to trace to retrieved tool output (FRE-1278).

    Even when web_search fired, retrieved and parametric (invented) names were blended with
    no distinction, so the fix must be to omit unsupported names, not merely label them.
    """
    assert _GROUNDING_RULE_FRAGMENT in _TOOL_RULES, (
        f"Grounding rule missing from _TOOL_RULES. Expected fragment: {_GROUNDING_RULE_FRAGMENT!r}"
    )


def test_grounding_rule_in_native_prompt() -> None:
    """TOOL_USE_NATIVE_PROMPT inherits the grounding rule from _TOOL_RULES."""
    assert _GROUNDING_RULE_FRAGMENT in TOOL_USE_NATIVE_PROMPT


def test_grounding_rule_in_injected_prompt() -> None:
    """TOOL_USE_PROMPT_INJECTED inherits the grounding rule from _TOOL_RULES."""
    assert _GROUNDING_RULE_FRAGMENT in TOOL_USE_PROMPT_INJECTED


def test_grounding_rule_says_omit_not_label() -> None:
    """The grounding rule must not offer 'mark it unverified' as an alternative to omitting.

    A prior draft allowed memory-supplied names if labeled unverified, which contradicts the
    ticket's AC-2 (any unsupported name is a fail even if labeled). The shipped rule must tell
    the model to omit unsupported names outright.
    """
    assert "Omit any such name you cannot point to in the results" in _TOOL_RULES


def test_entity_naming_worked_example_in_injected_prompt() -> None:
    """TOOL_USE_PROMPT_INJECTED includes a non-technical, entity-naming worked example.

    Both prior examples (FastAPI, React vs Svelte) were technical questions, which taught
    that search is for technical questions only (FRE-1278).
    """
    assert "tinned tuna" in TOOL_USE_PROMPT_INJECTED
