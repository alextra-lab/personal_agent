"""The worker tool split (FRE-1564, ADR-0150 D2 amendment of 2026-10-10).

The rule: no worker type holds both an outbound channel (a query or URL leaves the system)
and a private or internal read made with a tool. A hostile page cannot make a worker that has
no private tool query those stores, and a worker that holds a private tool has no way out.
The conversation context is the second half of the same rule: a type that holds an outbound
tool is briefed by its task text alone (owner decision A, 2026-10-10), and
`test_expansion_controller.py::TestWorkerConversationContext` checks what the model receives.

AC-2: each added tool reaches its worker type, after governance, and no write tool does.
AC-3: the rule, with a seeded negative.
AC-4 (unit half): the planner prompt carries the new descriptions and tools.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import pytest

from personal_agent.config.governance_loader import load_governance_config
from personal_agent.governance.models import Mode
from personal_agent.governance.sub_agent_tools import evaluate_sub_agent_tool_grant
from personal_agent.orchestrator.expansion_controller import _build_planner_system_prompt
from personal_agent.orchestrator.worker_types import (
    OUTBOUND_TOOLS,
    WORKER_TYPES,
    WorkerType,
    WorkerTypeSpec,
    carries_conversation_context,
)
from personal_agent.tools import register_mvp_tools
from personal_agent.tools.registry import ToolRegistry

# A worker tool is exactly one of these. A new worker tool that is in none of them fails
# `test_every_worker_tool_is_classified`, so the author must decide which side it is on.
OUTBOUND = OUTBOUND_TOOLS
# `read` reads local files (ADR-0156 D9.4: only the files a task names), so it is private.
PRIVATE = frozenset(
    {
        "search_memory",
        "recall_personal_history",
        "notes_search",
        "search_logs",
        "measure_model_latency",
        "read",
    }
)
# Reads nothing private and sends nothing out. `run_python` is here only because its
# `network` argument is pinned false for a worker (see the governance decision).
NEUTRAL = frozenset({"run_python", "read_skill"})

# Tools that change state, send a message, or drive a browser. None may reach a worker.
SIDE_EFFECTING = frozenset(
    {
        "bash",
        "write",
        "edit",
        "write_file",
        "notes_write",
        "artifact_write",
        "artifact_draft",
        "create_linear_issue",
        "create_linear_project",
        "perplexity_query",
    }
)


def types_holding_outbound_and_private(
    registry: Mapping[WorkerType, WorkerTypeSpec],
) -> list[WorkerType]:
    """Return the worker types whose tool list joins an outbound tool and a private read.

    Args:
        registry: A worker registry.

    Returns:
        Every offending type, in registry order. Empty when the rule holds.
    """
    return [
        worker_type
        for worker_type, spec in registry.items()
        if OUTBOUND & set(spec.tools) and PRIVATE & set(spec.tools)
    ]


class TestTheRule:
    def test_no_shipped_type_holds_an_outbound_tool_and_a_private_read(self) -> None:
        assert types_holding_outbound_and_private(WORKER_TYPES) == []

    @pytest.mark.parametrize("outbound_tool", sorted(OUTBOUND))
    def test_seeded_negative_an_outbound_tool_added_to_general_is_caught(
        self, outbound_tool: str
    ) -> None:
        """*Fails if* the rule cannot see a violation: adding fetch_url to `general` must trip it."""
        general = WORKER_TYPES[WorkerType.GENERAL]
        broken = {
            **WORKER_TYPES,
            WorkerType.GENERAL: replace(general, tools=(*general.tools, outbound_tool)),
        }
        assert types_holding_outbound_and_private(broken) == [WorkerType.GENERAL]

    def test_seeded_negative_a_private_read_added_to_researcher_is_caught(self) -> None:
        researcher = WORKER_TYPES[WorkerType.RESEARCHER]
        broken = {
            **WORKER_TYPES,
            WorkerType.RESEARCHER: replace(
                researcher, tools=(*researcher.tools, "search_logs")
            ),
        }
        assert types_holding_outbound_and_private(broken) == [WorkerType.RESEARCHER]

    def test_every_worker_tool_is_classified(self) -> None:
        held = {tool for spec in WORKER_TYPES.values() for tool in spec.tools}
        classified = OUTBOUND | PRIVATE | NEUTRAL
        assert held <= classified, f"unclassified worker tools: {sorted(held - classified)}"
        assert not (OUTBOUND & PRIVATE or OUTBOUND & NEUTRAL or PRIVATE & NEUTRAL)

    def test_run_python_cannot_reach_the_network_from_a_worker(self) -> None:
        """`general` holds private reads, so its code sandbox must not have a way out."""
        decision = load_governance_config().sub_agent_tools["run_python"]
        assert decision.param_forced == {"network": False}


class TestConversationContextRule:
    """Owner decision A (2026-10-10): a type that can send text out holds no conversation history."""

    def test_the_researcher_carries_none_and_general_carries_its_messages(self) -> None:
        assert carries_conversation_context(WORKER_TYPES[WorkerType.RESEARCHER]) is False
        assert carries_conversation_context(WORKER_TYPES[WorkerType.GENERAL]) is True

    @pytest.mark.parametrize("worker_type", list(WorkerType))
    def test_the_rule_is_exactly_no_outbound_tool(self, worker_type: WorkerType) -> None:
        spec = WORKER_TYPES[worker_type]
        assert carries_conversation_context(spec) is (not OUTBOUND & set(spec.tools))

    @pytest.mark.parametrize("outbound_tool", sorted(OUTBOUND))
    def test_seeded_negative_an_outbound_tool_added_to_general_removes_its_context(
        self, outbound_tool: str
    ) -> None:
        general = WORKER_TYPES[WorkerType.GENERAL]
        widened = replace(general, tools=(*general.tools, outbound_tool))
        assert carries_conversation_context(widened) is False

    def test_the_outbound_set_names_every_tool_that_sends_a_query_or_url_out(self) -> None:
        assert OUTBOUND_TOOLS == frozenset({"web_search", "fetch_url", "get_library_docs"})


class TestToolsReachTheWorkers:
    """AC-2 — the granted list, after governance, holds each added tool and no write tool."""

    _ADDED = {
        WorkerType.RESEARCHER: ("fetch_url", "get_library_docs", "read_skill"),
        WorkerType.GENERAL: (
            "search_logs",
            "measure_model_latency",
            "notes_search",
            "read_skill",
            "read",
        ),
    }

    @pytest.mark.parametrize("worker_type", list(WorkerType))
    def test_governance_grants_every_listed_tool_in_normal_mode(
        self, worker_type: WorkerType
    ) -> None:
        spec = WORKER_TYPES[worker_type]
        grant = evaluate_sub_agent_tool_grant(spec.tools, Mode.NORMAL, load_governance_config())
        assert grant.denied == ()
        assert grant.granted == spec.tools
        for added in self._ADDED[worker_type]:
            assert added in grant.granted

    @pytest.mark.parametrize("worker_type", list(WorkerType))
    def test_no_side_effecting_tool_is_listed_or_granted(self, worker_type: WorkerType) -> None:
        spec = WORKER_TYPES[worker_type]
        grant = evaluate_sub_agent_tool_grant(spec.tools, Mode.NORMAL, load_governance_config())
        assert not SIDE_EFFECTING & set(spec.tools)
        assert not SIDE_EFFECTING & set(grant.granted)

    def test_no_tool_is_granted_to_a_worker_outside_the_registry_lists(self) -> None:
        """The grant set may not outgrow the two type lists (a grant no type asks for is dead)."""
        config = load_governance_config()
        listed = {tool for spec in WORKER_TYPES.values() for tool in spec.tools}
        assert set(config.granted_sub_agent_tool_names()) == listed

    @pytest.mark.parametrize("mode", [Mode.ALERT, Mode.DEGRADED])
    @pytest.mark.parametrize("worker_type", list(WorkerType))
    def test_the_denied_modes_still_revoke_every_added_tool(
        self, worker_type: WorkerType, mode: Mode
    ) -> None:
        spec = WORKER_TYPES[worker_type]
        grant = evaluate_sub_agent_tool_grant(spec.tools, mode, load_governance_config())
        assert grant.granted == ()

    def test_the_default_registry_holds_the_tools_a_worker_is_offered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The F4 root cause: a granted name no registry holds never reaches the model.

        `run_python` and `read_skill` register with the primitive tools, which production
        enables. `notes_search` registers only when R2 is configured, so it is excluded here
        (the governance decision says the same).
        """
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "primitive_tools_enabled", True)
        registry = ToolRegistry()
        register_mvp_tools(registry)
        offered = {
            d["function"]["name"]
            # revealed=None: a worker holds the skill-provided tools from its closed list.
            for d in registry.get_tool_definitions_for_llm(
                mode=None, include_worker_only=True, revealed=None
            )
        }
        listed = {tool for spec in WORKER_TYPES.values() for tool in spec.tools}
        conditional = {"notes_search"}
        assert (listed - conditional) <= offered, sorted((listed - conditional) - offered)

    @pytest.mark.parametrize("mode", [Mode.NORMAL, Mode.ALERT, Mode.DEGRADED, Mode.RECOVERY])
    def test_the_primary_is_offered_the_telemetry_tools_only_after_the_reveal(
        self, monkeypatch: pytest.MonkeyPatch, mode: Mode
    ) -> None:
        """ADR-0156 A5.3 (FRE-1596 AC-3): the telemetry tools are skill-provided.

        ``get_tool_definitions_for_llm(mode=ctx.mode, revealed=...)`` is the call the primary's turn
        makes (``orchestrator/executor.py``). Before the reveal the list holds neither tool.
        """
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "primitive_tools_enabled", True)
        registry = ToolRegistry()
        register_mvp_tools(registry)
        tools = ("search_logs", "measure_model_latency")
        before = {d["function"]["name"] for d in registry.get_tool_definitions_for_llm(mode)}
        assert not set(tools) & before
        after = {
            d["function"]["name"]
            for d in registry.get_tool_definitions_for_llm(mode, revealed=set(tools))
        }
        assert set(tools) <= after

    def test_the_worker_loop_builds_the_declaration_from_the_full_registry(self) -> None:
        from unittest.mock import MagicMock, patch

        from personal_agent.orchestrator.sub_agent import _build_tool_defs

        registry = ToolRegistry()
        register_mvp_tools(registry)
        layer = MagicMock()
        layer.registry = registry
        with patch(
            "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
            return_value=layer,
        ):
            defs = _build_tool_defs(["search_logs", "measure_model_latency", "web_search"])
        assert defs is not None
        assert {d["function"]["name"] for d in defs} == {
            "search_logs",
            "measure_model_latency",
            "web_search",
        }


class TestPlannerPrompt:
    """AC-4 (unit half) — the planner is told what each type is for and what it holds."""

    @staticmethod
    def _prompt() -> str:
        grant = evaluate_sub_agent_tool_grant(
            tuple({t for spec in WORKER_TYPES.values() for t in spec.tools}),
            Mode.NORMAL,
            load_governance_config(),
        )
        return _build_planner_system_prompt(list(grant.granted))

    @staticmethod
    def _line(prompt: str, worker_type: str) -> str:
        return next(ln for ln in prompt.splitlines() if ln.startswith(f"  - {worker_type}: "))

    def test_general_is_described_as_the_type_for_the_systems_own_telemetry(self) -> None:
        line = self._line(self._prompt(), "general")
        assert "the system's own logs, metrics, errors and health" in line
        assert "search_logs" in line
        assert "measure_model_latency" in line

    def test_researcher_is_described_as_reading_a_known_page_or_library_docs(self) -> None:
        line = self._line(self._prompt(), "researcher")
        assert "reads a known web page or library documentation" in line
        assert "fetch_url" in line
        assert "get_library_docs" in line

    def test_neither_line_advertises_the_other_sides_tools(self) -> None:
        prompt = self._prompt()
        researcher, general = self._line(prompt, "researcher"), self._line(prompt, "general")
        assert "search_logs" not in researcher
        assert "measure_model_latency" not in researcher
        assert "fetch_url" not in general
