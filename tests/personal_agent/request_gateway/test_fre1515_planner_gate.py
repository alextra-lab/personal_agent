"""FRE-1515 AC-1 (ADR-0154 D2) — the four register types reach the planner, only behind the gate.

The gate is one boolean: does the session's primary deployment declare a ``planner`` mode in
``config/models.yaml``. With it, ``CONVERSATIONAL``, ``TOOL_USE``, ``ANALYSIS`` and ``PLANNING``
route ``HYBRID`` with reason ``planner_asked`` at every complexity. Without it, they route as
before, and ``planner_gate_reason`` records ``planner_mode_absent``. The other types and the
resource forcings do not change.
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import product
from pathlib import Path

import pytest

from personal_agent.config.model_loader import load_model_config, primary_declares_planner_mode
from personal_agent.config.selection import _current_selection, set_current_selection
from personal_agent.governance.models import Mode
from personal_agent.llm_client.models import ModelConfig
from personal_agent.request_gateway.decomposition import assess_decomposition
from personal_agent.request_gateway.types import (
    Complexity,
    DecompositionStrategy,
    GovernanceContext,
    IntentResult,
    TaskType,
)

_CATALOG_PATH = Path(__file__).resolve().parents[3] / "config" / "models.yaml"

REGISTER_TYPES = (TaskType.CONVERSATIONAL, TaskType.TOOL_USE, TaskType.ANALYSIS, TaskType.PLANNING)

S, H, D = (
    DecompositionStrategy.SINGLE,
    DecompositionStrategy.HYBRID,
    DecompositionStrategy.DECOMPOSE,
)

#: Today's routing of the four types, written out by hand so the test does not read the code
#: it checks (ADR-0154 D2: "the four types route exactly as they do today").
TODAY: dict[tuple[TaskType, Complexity], tuple[DecompositionStrategy, str]] = {
    (TaskType.CONVERSATIONAL, Complexity.SIMPLE): (S, "conversational_always_single"),
    (TaskType.CONVERSATIONAL, Complexity.MODERATE): (S, "conversational_always_single"),
    (TaskType.CONVERSATIONAL, Complexity.COMPLEX): (S, "conversational_always_single"),
    (TaskType.TOOL_USE, Complexity.SIMPLE): (S, "tool_use_single"),
    (TaskType.TOOL_USE, Complexity.MODERATE): (S, "tool_use_single"),
    (TaskType.TOOL_USE, Complexity.COMPLEX): (S, "tool_use_single"),
    (TaskType.ANALYSIS, Complexity.SIMPLE): (S, "analysis_simple"),
    (TaskType.ANALYSIS, Complexity.MODERATE): (H, "analysis_moderate_hybrid"),
    (TaskType.ANALYSIS, Complexity.COMPLEX): (D, "analysis_complex_decompose"),
    (TaskType.PLANNING, Complexity.SIMPLE): (S, "planning_simple"),
    (TaskType.PLANNING, Complexity.MODERATE): (H, "planning_moderate_hybrid"),
    (TaskType.PLANNING, Complexity.COMPLEX): (H, "planning_moderate_hybrid"),
}

#: The types D2 leaves alone, with the gate open or closed (delegation adapter not wired).
UNCHANGED: dict[tuple[TaskType, Complexity], tuple[DecompositionStrategy, str]] = {
    **{(TaskType.MEMORY_RECALL, c): (S, "memory_recall_always_single") for c in Complexity},
    **{(TaskType.SELF_IMPROVE, c): (S, "self_improve_always_single") for c in Complexity},
    (TaskType.DELEGATION, Complexity.SIMPLE): (S, "delegation_no_target_fallback_single"),
    (TaskType.DELEGATION, Complexity.MODERATE): (H, "delegation_no_target_fallback_hybrid"),
    (TaskType.DELEGATION, Complexity.COMPLEX): (D, "delegation_no_target_fallback_decompose"),
}


def _intent(task_type: TaskType, complexity: Complexity) -> IntentResult:
    return IntentResult(task_type=task_type, complexity=complexity, confidence=0.9, signals=[])


def _governance(*, permitted: bool = True, budget: int = 3) -> GovernanceContext:
    return GovernanceContext(
        mode=Mode.NORMAL, expansion_permitted=permitted, expansion_budget=budget
    )


class TestTheMatrix:
    """Every combination of the four types, every complexity and both gate values."""

    @pytest.mark.parametrize(("task_type", "complexity"), list(product(REGISTER_TYPES, Complexity)))
    def test_with_a_planner_mode_the_planner_is_asked(
        self, task_type: TaskType, complexity: Complexity
    ) -> None:
        result = assess_decomposition(
            _intent(task_type, complexity), _governance(), planner_mode_declared=True
        )
        assert (result.strategy, result.reason) == (H, "planner_asked")
        assert result.planner_gate_reason is None

    @pytest.mark.parametrize(("task_type", "complexity"), list(product(REGISTER_TYPES, Complexity)))
    def test_without_a_planner_mode_routing_is_today_s(
        self, task_type: TaskType, complexity: Complexity
    ) -> None:
        result = assess_decomposition(
            _intent(task_type, complexity), _governance(), planner_mode_declared=False
        )
        assert (result.strategy, result.reason) == TODAY[(task_type, complexity)]
        assert result.planner_gate_reason == "planner_mode_absent"

    def test_the_table_covers_every_case(self) -> None:
        assert set(TODAY) == set(product(REGISTER_TYPES, Complexity))
        others = [t for t in TaskType if t not in REGISTER_TYPES]
        assert set(UNCHANGED) == set(product(others, Complexity))

    @pytest.mark.parametrize(
        ("task_type", "complexity", "gate"),
        [(t, c, g) for (t, c) in UNCHANGED for g in (True, False)],
    )
    def test_the_other_types_do_not_change(
        self, task_type: TaskType, complexity: Complexity, gate: bool
    ) -> None:
        result = assess_decomposition(
            _intent(task_type, complexity), _governance(), planner_mode_declared=gate
        )
        assert (result.strategy, result.reason) == UNCHANGED[(task_type, complexity)]
        assert result.planner_gate_reason is None

    def test_a_wired_delegation_adapter_still_delegates(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.DELEGATION, Complexity.SIMPLE),
            _governance(),
            delegation_enabled=True,
            planner_mode_declared=True,
        )
        assert (result.strategy, result.reason) == (
            DecompositionStrategy.DELEGATE,
            "delegation_route_external",
        )

    @pytest.mark.parametrize(
        ("task_type", "complexity", "gate"),
        list(product(REGISTER_TYPES, Complexity, (True, False))),
    )
    @pytest.mark.parametrize(
        ("governance", "expansion_enabled", "reason"),
        [
            (_governance(permitted=False), True, "expansion_denied"),
            (_governance(budget=0), True, "zero_budget"),
            (_governance(), False, "expansion_disabled"),
        ],
    )
    def test_the_forcings_still_force_single_first(
        self,
        task_type: TaskType,
        complexity: Complexity,
        gate: bool,
        governance: GovernanceContext,
        expansion_enabled: bool,
        reason: str,
    ) -> None:
        result = assess_decomposition(
            _intent(task_type, complexity),
            governance,
            expansion_enabled=expansion_enabled,
            planner_mode_declared=gate,
        )
        assert (result.strategy, result.reason) == (S, reason)
        # ADR-0154 D6: the gate reason is recorded on a forced turn too.
        assert result.planner_gate_reason == (None if gate else "planner_mode_absent")


@pytest.fixture
def catalog() -> ModelConfig:
    return load_model_config(_CATALOG_PATH)


@pytest.fixture(autouse=True)
def _reset_selection() -> Iterator[None]:
    token = _current_selection.set({})
    try:
        yield
    finally:
        _current_selection.reset(token)


def _with_planner_mode(config: ModelConfig) -> list[str]:
    return [key for key, d in config.models.items() if "planner" in d.modes]


def _selectable_without_planner_mode(config: ModelConfig) -> str:
    from personal_agent.config.model_loader import is_selectable_binding

    return next(
        key
        for key, d in config.models.items()
        if "planner" not in d.modes and is_selectable_binding("primary", key, config)
    )


class TestTheGateResolvesTheTurnsPrimary:
    """The gate reads the deployment the ``primary`` client resolves to for this turn."""

    def test_a_selected_primary_with_a_planner_mode_opens_the_gate(
        self, catalog: ModelConfig
    ) -> None:
        key = _with_planner_mode(catalog)[0]
        set_current_selection({"primary": key})
        assert primary_declares_planner_mode(catalog) is True

    def test_a_selected_primary_without_one_closes_it(self, catalog: ModelConfig) -> None:
        set_current_selection({"primary": _selectable_without_planner_mode(catalog)})
        assert primary_declares_planner_mode(catalog) is False

    def test_no_selection_reads_the_binding_default(self, catalog: ModelConfig) -> None:
        default = catalog.roles["primary"].deployment
        expected = "planner" in catalog.models[default].modes
        assert primary_declares_planner_mode(catalog) is expected

    def test_the_gate_matches_the_client_the_planner_call_builds(
        self, catalog: ModelConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same deployment, same mode test as ``get_llm_client(..., mode="planner")``."""
        from personal_agent.llm_client import factory

        monkeypatch.setattr(factory, "load_model_config", lambda *a, **k: catalog)
        for key in [*_with_planner_mode(catalog), _selectable_without_planner_mode(catalog)]:
            set_current_selection({"primary": key})
            client = factory.get_llm_client(role_name="primary", mode="planner")
            ran_planner_mode = client.model_def.default_mode == "planner"
            assert primary_declares_planner_mode(catalog) is ran_planner_mode, key

    def test_a_catalog_error_closes_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _broken(*_a: object, **_k: object) -> ModelConfig:
            raise RuntimeError("catalog unreadable")

        monkeypatch.setattr("personal_agent.config.model_loader.load_model_config", _broken)
        import structlog.testing

        with structlog.testing.capture_logs() as logs:
            assert primary_declares_planner_mode(trace_id="t-1515") is False
        failed = [e for e in logs if e["event"] == "planner_gate_resolve_failed"]
        assert failed and failed[0]["trace_id"] == "t-1515"


#: The deployments whose ``planner`` mode passed ADR-0154 D7 on the current planner prompt
#: (Amendment 1, section 1.2, and AC-10). qwen3.8-flash-next: FRE-1581 (18 tools), then
#: FRE-1590's long-history and paired arms with the FRE-1515 decide rerun (24 tools, 44/48).
#: gemma-4-26b-a4b failed D7 (11/13, decline-correct 24/30), so by the owner's decision "A."
#: on FRE-1581 its planner mode is removed. A new planner mode needs a passing D7 run first.
QUALIFIED_PLANNER_DEPLOYMENTS = {"qwen3.8-flash-next"}


def test_only_a_qualified_deployment_declares_a_planner_mode(catalog: ModelConfig) -> None:
    """A planner mode opens the gate, so the catalog may hold one only where D7 passed."""
    assert set(_with_planner_mode(catalog)) == QUALIFIED_PLANNER_DEPLOYMENTS
