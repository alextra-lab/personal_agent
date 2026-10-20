"""FRE-1515 (ADR-0154 D2, D7, AC-10) — the decline prompt that ships is the prompt the probe qualifies.

On a ``planner_asked`` turn the planner system prompt carries the FRE-1498 decline rule and a
schema that admits ``SINGLE``. The D7 probe renders that prompt in
``scripts/eval/fre1537/render.py`` and records its SHA-256 in the configuration fingerprint.
If production rendered it differently, the qualified hash would not be the deployed one.
"""

from __future__ import annotations

import pytest
from scripts.eval.fre1537 import render

from personal_agent.config.seshat_effort import (
    EffortLevel,
    effort_spec,
    reset_current_effort,
    set_current_effort,
)
from personal_agent.orchestrator.expansion_controller import (
    _P2_DECOMPOSITION_POLICY,
    _build_planner_system_prompt,
)

SURFACES = [
    [],
    ["web_search"],
    ["web_search", "fetch_url", "read", "search_memory"],
]


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("level", ["fast", "standard", "deep", None])
def test_the_decline_prompt_equals_the_probe_rendering(
    surface: list[str], level: EffortLevel | None
) -> None:
    """FRE-1622 (ADR-0157 D7 rule 1): every level's D2 prompt, rendered as the turn renders it."""
    token = set_current_effort(effort_spec(level))
    try:
        production = _build_planner_system_prompt(surface, admit_single=True)
    finally:
        reset_current_effort(token)
    assert production == render.render_system_prompt(surface, level)
    assert render.prompt_hash(production) == render.prompt_hash(
        render.render_system_prompt(surface, level)
    )


def test_the_probe_carries_the_production_p2_block() -> None:
    assert render.P2_BLOCK == _P2_DECOMPOSITION_POLICY


@pytest.mark.parametrize("surface", SURFACES)
def test_without_the_flag_the_prompt_offers_no_decline(surface: list[str]) -> None:
    prompt = _build_planner_system_prompt(surface)
    assert prompt == _build_planner_system_prompt(surface, admit_single=False)
    assert "SINGLE" not in prompt
    assert '"strategy": "HYBRID|DECOMPOSE"' in prompt


def test_the_decline_prompt_carries_the_fre1498_rule_unchanged() -> None:
    prompt = _build_planner_system_prompt(["web_search"], admit_single=True)
    assert render.DECLINE_RULE in prompt
    assert '"strategy": "SINGLE|HYBRID|DECOMPOSE"' in prompt
    # The rule is the first rule, as the probe placed it.
    assert prompt.index("Rules:\n") + len("Rules:\n") == prompt.index(render.DECLINE_RULE)
