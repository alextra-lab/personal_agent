"""FRE-1622 (ADR-0157 D3 item 2, D6): the planner prompt renders the turn's level (AC-2, AC-5).

``fixtures/planner_prompt_pre_fre1622.json`` holds the prompts that the renderer of ``main`` at
``ca34fedc`` gave at 12 and 20 rounds, before this change. It is static test data: never
regenerate it from the code under test, or the byte-identity checks compare the code with itself.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from personal_agent.config import get_settings
from personal_agent.config.seshat_effort import (
    EFFORT_LEVELS,
    EffortSpec,
    effort_spec,
    reset_current_effort,
    set_current_effort,
)
from personal_agent.orchestrator.expansion_controller import (
    _P2_DECOMPOSITION_POLICY,
    _PLANNER_RULES_HEADING,
    _build_planner_system_prompt,
)

_FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "planner_prompt_pre_fre1622.json").read_text()
)
_SURFACES: dict[str, list[str]] = _FIXTURES["surfaces"]
_PRE: dict[str, str] = _FIXTURES["prompts"]
_ADR = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "architecture_decisions"
    / "ADR-0157-seshat-effort-one-level-per-session.md"
)


@pytest.fixture(autouse=True)
def _ceiling_unlike_every_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "expansion_enabled", True)
    monkeypatch.setattr(get_settings(), "sub_agent_max_tool_iterations", 30)


def _with_policy(level: str, policy: str) -> EffortSpec:
    return effort_spec(level).model_copy(update={"planner_policy": policy})  # type: ignore[arg-type]


class TestTheP2Block:
    def test_it_is_the_adr_text_word_for_word(self) -> None:
        match = re.search(r"```text\n(DECOMPOSITION POLICY\n.*?)```", _ADR.read_text(), re.S)
        assert match is not None
        assert match.group(1) == _P2_DECOMPOSITION_POLICY

    def test_it_has_913_characters_in_23_lines(self) -> None:
        assert len(_P2_DECOMPOSITION_POLICY) == 913
        lines = _P2_DECOMPOSITION_POLICY.split("\n")
        assert lines[-1] == "" and len(lines) == 24


class TestPlacement:
    @pytest.mark.parametrize("surface", list(_SURFACES))
    @pytest.mark.parametrize("level", ["standard", "deep"])
    def test_p2_is_the_baseline_with_the_block_before_the_first_rules_heading(
        self, surface: str, level: str
    ) -> None:
        tools = _SURFACES[surface]
        baseline = _build_planner_system_prompt(
            tools, admit_single=True, effort=_with_policy(level, "baseline")
        )
        p2 = _build_planner_system_prompt(
            tools, admit_single=True, effort=_with_policy(level, "p2")
        )
        at = baseline.index(_PLANNER_RULES_HEADING)
        assert p2 == baseline[:at] + _P2_DECOMPOSITION_POLICY + "\n" + baseline[at:]
        assert len(p2) - len(baseline) == 914
        # The FRE-1498 decline rule stays the first rule.
        assert p2.index("Rules:\n- First decide whether") > p2.index("DECOMPOSITION POLICY")

    @pytest.mark.parametrize("surface", list(_SURFACES))
    @pytest.mark.parametrize("level", list(EFFORT_LEVELS))
    @pytest.mark.parametrize("policy", ["baseline", "p2"])
    def test_without_admit_single_no_level_carries_the_block(
        self, surface: str, level: str, policy: str
    ) -> None:
        spec = effort_spec(level).model_copy(update={"planner_policy": policy})  # type: ignore[arg-type]
        prompt = _build_planner_system_prompt(_SURFACES[surface], admit_single=False, effort=spec)
        assert "DECOMPOSITION POLICY" not in prompt


class TestByteIdentityWithThePreChangeRenderer:
    @pytest.mark.parametrize("surface", list(_SURFACES))
    @pytest.mark.parametrize("admit_single", [True, False])
    def test_baseline_at_12_rounds_is_the_pre_change_prompt(
        self, surface: str, admit_single: bool
    ) -> None:
        prompt = _build_planner_system_prompt(
            _SURFACES[surface],
            admit_single=admit_single,
            effort=_with_policy("standard", "baseline"),
        )
        assert prompt == _PRE[f"12|{surface}|{admit_single}"]

    @pytest.mark.parametrize("surface", list(_SURFACES))
    @pytest.mark.parametrize("admit_single", [True, False])
    def test_deep_is_the_pre_change_prompt_at_20_rounds(
        self, surface: str, admit_single: bool
    ) -> None:
        prompt = _build_planner_system_prompt(
            _SURFACES[surface], admit_single=admit_single, effort=effort_spec("deep")
        )
        assert prompt == _PRE[f"20|{surface}|{admit_single}"]

    @pytest.mark.parametrize("surface", list(_SURFACES))
    def test_standard_as_committed(self, surface: str) -> None:
        """AC-5 against the committed ``planner_policy`` of Standard."""
        standard = effort_spec("standard")
        prompt = _build_planner_system_prompt(
            _SURFACES[surface], admit_single=True, effort=standard
        )
        pre = _PRE[f"12|{surface}|True"]
        if standard.planner_policy == "p2":
            at = pre.index(_PLANNER_RULES_HEADING)
            assert prompt == pre[:at] + _P2_DECOMPOSITION_POLICY + "\n" + pre[at:]
        else:
            assert prompt == pre


class TestTheRoundsComeFromTheSpec:
    @pytest.fixture
    def current(self, request: pytest.FixtureRequest) -> Iterator[str]:
        token = set_current_effort(effort_spec(request.param))
        try:
            yield request.param
        finally:
            reset_current_effort(token)

    @pytest.mark.parametrize(
        ("current", "rounds"), [("standard", 12), ("deep", 20)], indirect=["current"]
    )
    def test_the_prompt_states_the_level_budget_not_the_ceiling(
        self, current: str, rounds: int
    ) -> None:
        # No explicit spec: the renderer reads the turn's spec, as _run_planner does.
        prompt = _build_planner_system_prompt(["web_search"], admit_single=True)
        assert f"({rounds} tool round(s) each)" in prompt
        assert "(30 tool round(s) each)" not in prompt
