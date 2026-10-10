"""The worker tool split (FRE-1564, FRE-1565, ADR-0150 D2 amendments of 2026-10-10).

The rule (FRE-1564): no worker type holds both an outbound channel (a query or URL leaves the
system) and a private or internal read made with a tool, unless the owner approves each call
that can send data out (FRE-1565, option A: "all tools, under controls"). A hostile page cannot
make a worker that has no private tool query those stores, and a worker that holds a private
read has no way out that runs without the owner's per-call approval. The conversation context
is the second half of the same rule: a type that holds an outbound tool is briefed by its task
text alone (owner decision A, 2026-10-10), and
`test_expansion_controller.py::TestWorkerConversationContext` checks what the model receives.

FRE-1564 AC-2: each added tool reaches its worker type, after governance.
FRE-1564 AC-3 / FRE-1565 AC-3: the rule, with seeded negatives.
FRE-1564 AC-4 (unit half): the planner prompt carries the descriptions and tools.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import pytest

from personal_agent.config.governance_loader import load_governance_config
from personal_agent.governance.models import GovernanceConfig, Mode
from personal_agent.governance.sub_agent_tools import (
    evaluate_sub_agent_tool_grant,
    sub_agent_tool_asks_per_call,
    sub_agent_tool_requires_approval,
)
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

# A worker tool is in one of these. A new worker tool that is in none of them fails
# `test_every_worker_tool_is_classified`, so the author must decide which side it is on.
OUTBOUND = OUTBOUND_TOOLS
# `bash` is in both classes: it reads files and the environment, and it reaches the network.
PRIVATE = frozenset(
    {"search_memory", "recall_personal_history", "notes_search", "query_telemetry", "bash"}
)
# Writes durable owner data that later turns read back (FRE-1565). A worker must ask per call.
DURABLE_WRITE = frozenset({"notes_write", "artifact_write"})
# Reads nothing private and sends nothing out. `run_python` is here only because its
# `network` argument is pinned false for a worker (see the governance decision). `write` is
# here because a worker's `write` is confined to its own workspace (FRE-1565).
NEUTRAL = frozenset({"run_python", "read_skill", "write"})

# Tools that change state, send a message, or drive a browser. Only `operator` holds them.
SIDE_EFFECTING = frozenset(
    {
        "bash",
        "write",
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
        Every such type, in registry order.
    """
    return [
        worker_type
        for worker_type, spec in registry.items()
        if OUTBOUND & set(spec.tools) and PRIVATE & set(spec.tools)
    ]


def unapproved_exfiltration_paths(
    registry: Mapping[WorkerType, WorkerTypeSpec],
    config: GovernanceConfig,
) -> list[tuple[WorkerType, str]]:
    """Return every call that can send data out after a private read without per-call approval.

    FRE-1565 AC-3. A type that holds a private read and an outbound tool is allowed only when
    each outbound tool it holds asks the owner on every call, in NORMAL too. A durable write
    must always ask per call.

    Args:
        registry: A worker registry.
        config: A governance configuration.

    Returns:
        ``(type, tool)`` for each offending tool, in registry order. Empty when the rule holds.
    """
    found: list[tuple[WorkerType, str]] = []
    for worker_type, spec in registry.items():
        held = set(spec.tools)
        exposed = OUTBOUND & held if PRIVATE & held else set()
        for tool in sorted(exposed | (DURABLE_WRITE & held)):
            if not (
                sub_agent_tool_asks_per_call(tool, config)
                and sub_agent_tool_requires_approval(tool, Mode.NORMAL, config)
            ):
                found.append((worker_type, tool))
    return found


def _with_decision(config: GovernanceConfig, tool: str, **update: object) -> GovernanceConfig:
    """Return a copy of ``config`` with one sub-agent decision changed (a seeded negative)."""
    decisions = dict(config.sub_agent_tools)
    decisions[tool] = decisions[tool].model_copy(update=update)
    return config.model_copy(update={"sub_agent_tools": decisions})


class TestTheRule:
    def test_no_shipped_type_has_an_unapproved_exfiltration_path(self) -> None:
        assert unapproved_exfiltration_paths(WORKER_TYPES, load_governance_config()) == []

    def test_the_two_unapproved_types_keep_the_fre1564_split(self) -> None:
        """`researcher` and `general` ask nothing per call, so the plain split must hold."""
        assert types_holding_outbound_and_private(WORKER_TYPES) == [WorkerType.OPERATOR]

    def test_seeded_negative_bash_approved_per_turn_is_caught(self) -> None:
        """*Fails if* a policy change lets a worker bash call run on a turn-wide "yes"."""
        broken = _with_decision(load_governance_config(), "bash", approval="policy")
        assert (WorkerType.OPERATOR, "bash") in unapproved_exfiltration_paths(
            WORKER_TYPES, broken
        )

    @pytest.mark.parametrize("tool", ["create_linear_issue", "notes_write"])
    def test_seeded_negative_a_side_effect_approved_per_turn_is_caught(self, tool: str) -> None:
        broken = _with_decision(load_governance_config(), tool, approval="policy")
        assert (WorkerType.OPERATOR, tool) in unapproved_exfiltration_paths(WORKER_TYPES, broken)

    def test_seeded_negative_an_unapproved_outbound_tool_added_to_operator_is_caught(
        self,
    ) -> None:
        operator = WORKER_TYPES[WorkerType.OPERATOR]
        broken = {
            **WORKER_TYPES,
            WorkerType.OPERATOR: replace(operator, tools=(*operator.tools, "web_search")),
        }
        assert unapproved_exfiltration_paths(broken, load_governance_config()) == [
            (WorkerType.OPERATOR, "web_search")
        ]

    @pytest.mark.parametrize("outbound_tool", ["web_search", "fetch_url", "get_library_docs"])
    def test_seeded_negative_an_outbound_tool_added_to_general_is_caught(
        self, outbound_tool: str
    ) -> None:
        """*Fails if* the rule cannot see a violation: adding fetch_url to `general` must trip it."""
        general = WORKER_TYPES[WorkerType.GENERAL]
        broken = {
            **WORKER_TYPES,
            WorkerType.GENERAL: replace(general, tools=(*general.tools, outbound_tool)),
        }
        assert unapproved_exfiltration_paths(broken, load_governance_config()) == [
            (WorkerType.GENERAL, outbound_tool)
        ]

    def test_seeded_negative_a_private_read_added_to_researcher_is_caught(self) -> None:
        researcher = WORKER_TYPES[WorkerType.RESEARCHER]
        broken = {
            **WORKER_TYPES,
            WorkerType.RESEARCHER: replace(
                researcher, tools=(*researcher.tools, "query_telemetry")
            ),
        }
        assert {
            worker_type for worker_type, _ in unapproved_exfiltration_paths(
                broken, load_governance_config()
            )
        } == {WorkerType.RESEARCHER}

    def test_every_worker_tool_is_classified(self) -> None:
        held = {tool for spec in WORKER_TYPES.values() for tool in spec.tools}
        classified = OUTBOUND | PRIVATE | NEUTRAL | DURABLE_WRITE
        assert held <= classified, f"unclassified worker tools: {sorted(held - classified)}"
        assert OUTBOUND & PRIVATE == {"bash"}
        assert not (OUTBOUND & NEUTRAL or PRIVATE & NEUTRAL)
        assert not (DURABLE_WRITE & (OUTBOUND | PRIVATE | NEUTRAL))

    def test_run_python_cannot_reach_the_network_from_a_worker(self) -> None:
        """`general` holds private reads, so its code sandbox must not have a way out."""
        decision = load_governance_config().sub_agent_tools["run_python"]
        assert decision.param_forced == {"network": False}


class TestConversationContextRule:
    """Owner decision A (2026-10-10): a type that can send text out holds no conversation history."""

    def test_the_researcher_carries_none_and_general_carries_its_messages(self) -> None:
        assert carries_conversation_context(WORKER_TYPES[WorkerType.RESEARCHER]) is False
        assert carries_conversation_context(WORKER_TYPES[WorkerType.GENERAL]) is True

    def test_the_operator_carries_none(self) -> None:
        """FRE-1565: `operator` holds bash and the Linear writes, so its task text is all it gets."""
        assert carries_conversation_context(WORKER_TYPES[WorkerType.OPERATOR]) is False

    @pytest.mark.parametrize("worker_type", list(WorkerType))
    def test_the_rule_is_exactly_no_outbound_tool(self, worker_type: WorkerType) -> None:
        spec = WORKER_TYPES[worker_type]
        assert carries_conversation_context(spec) is (not OUTBOUND & set(spec.tools))

    @pytest.mark.parametrize("outbound_tool", sorted(OUTBOUND - {"bash"}))
    def test_seeded_negative_an_outbound_tool_added_to_general_removes_its_context(
        self, outbound_tool: str
    ) -> None:
        general = WORKER_TYPES[WorkerType.GENERAL]
        widened = replace(general, tools=(*general.tools, outbound_tool))
        assert carries_conversation_context(widened) is False

    def test_the_outbound_set_names_every_tool_that_sends_a_query_or_url_out(self) -> None:
        assert OUTBOUND_TOOLS == frozenset(
            {
                "web_search",
                "fetch_url",
                "get_library_docs",
                "bash",
                "create_linear_issue",
                "create_linear_project",
            }
        )


class TestToolsReachTheWorkers:
    """AC-2 — the granted list, after governance, holds each added tool and no write tool."""

    _ADDED = {
        WorkerType.RESEARCHER: ("fetch_url", "get_library_docs"),
        WorkerType.GENERAL: ("query_telemetry", "notes_search", "read_skill"),
        WorkerType.OPERATOR: (
            "bash",
            "write",
            "create_linear_issue",
            "create_linear_project",
            "notes_write",
            "artifact_write",
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

    @pytest.mark.parametrize("worker_type", [WorkerType.RESEARCHER, WorkerType.GENERAL])
    def test_no_side_effecting_tool_is_listed_or_granted(self, worker_type: WorkerType) -> None:
        spec = WORKER_TYPES[worker_type]
        grant = evaluate_sub_agent_tool_grant(spec.tools, Mode.NORMAL, load_governance_config())
        assert not SIDE_EFFECTING & set(spec.tools)
        assert not SIDE_EFFECTING & set(grant.granted)

    def test_the_refused_side_effecting_tools_reach_no_worker(self) -> None:
        """FRE-1565: `read` and `artifact_draft` carry a refusal; the rest have no decision."""
        config = load_governance_config()
        granted = set(config.granted_sub_agent_tool_names())
        for tool in ("read", "artifact_draft", "write_file", "perplexity_query"):
            assert tool not in granted
        assert config.sub_agent_tools["read"].granted is False
        assert config.sub_agent_tools["artifact_draft"].granted is False

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
            for d in registry.get_tool_definitions_for_llm(mode=None, include_worker_only=True)
        }
        listed = {tool for spec in WORKER_TYPES.values() for tool in spec.tools}
        # These register only when the R2 substrate is configured.
        conditional = {"notes_search", "notes_write", "artifact_write"}
        assert (listed - conditional) <= offered, sorted((listed - conditional) - offered)

    def test_the_primary_is_not_offered_the_worker_only_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The primary keeps its bash route and its tool surface (and per-turn tokens)."""
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "primitive_tools_enabled", True)
        registry = ToolRegistry()
        register_mvp_tools(registry)
        primary = {
            d["function"]["name"] for d in registry.get_tool_definitions_for_llm(Mode.NORMAL)
        }
        assert "query_telemetry" not in primary
        assert "query_telemetry" in registry.list_tool_names()

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
            defs = _build_tool_defs(["query_telemetry", "web_search"])
        assert defs is not None
        assert {d["function"]["name"] for d in defs} == {"query_telemetry", "web_search"}


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
        assert "query_telemetry" in line

    def test_researcher_is_described_as_reading_a_known_page_or_library_docs(self) -> None:
        line = self._line(self._prompt(), "researcher")
        assert "reads a known web page or library documentation" in line
        assert "fetch_url" in line
        assert "get_library_docs" in line

    def test_neither_line_advertises_the_other_sides_tools(self) -> None:
        prompt = self._prompt()
        researcher, general = self._line(prompt, "researcher"), self._line(prompt, "general")
        assert "query_telemetry" not in researcher
        assert "fetch_url" not in general

    def test_operator_is_described_with_its_tools_and_the_approval(self) -> None:
        """FRE-1565: the planner learns there is a type for changes, and that each one asks."""
        prompt = self._prompt()
        operator = self._line(prompt, "operator")
        assert "Makes one bounded change the user asked for" in operator
        assert "The owner approves each such call" in operator
        for tool in ("bash", "write", "create_linear_issue", "notes_write", "artifact_write"):
            assert tool in operator
        for other in (self._line(prompt, "researcher"), self._line(prompt, "general")):
            assert "bash" not in other
            assert "create_linear_issue" not in other
