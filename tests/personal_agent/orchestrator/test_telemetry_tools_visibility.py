"""ADR-0156 A3–A5 (FRE-1596 AC-1 to AC-4): the telemetry tools are skill-provided, by objective.

The real skills and the real tool registry, not seeded ones:

- AC-1: no real skill name and no real tool name holds a backend word.
- AC-2: every real skill, and every skill-provided tool, has a "Not for…" line that names things
  that exist.
- AC-3: the first primary request holds neither ``search_logs`` nor ``measure_model_latency``, and a
  ``general`` worker's request holds both. Both requests are the real ones: the primary's
  ``step_llm_call`` and the worker loop run with a recording model client.
- AC-4: each of the two tools has a providing skill, and the provided set is exactly the two.

The live capture gateway run belongs to FRE-1597 (T-D1). These tests prove the request assembly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.config.governance_loader import load_governance_config
from personal_agent.governance.models import Mode
from personal_agent.governance.sub_agent_tools import evaluate_sub_agent_tool_grant
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.skills import (
    get_all_skills,
    name_violations,
    not_for_violations,
    provider_violations,
    assemble_skill_catalog,
    providers_of,
    tools_revealed_by,
    skill_names,
)
from personal_agent.orchestrator.sub_agent import SubAgentSpec
from personal_agent.orchestrator.types import ExecutionContext
from personal_agent.orchestrator.worker_types import WORKER_TYPES, WorkerType
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools import get_default_registry
from tests._helpers.worker_run import run_worker, shared_layer, worker_layer

_TOOLS = ("search_logs", "measure_model_latency")


def _registered() -> dict[str, bool]:
    """Every registered tool name, mapped to its ``skill_provided`` flag."""
    registry = get_default_registry()
    return {
        t.name: t.skill_provided
        for t in registry.list_tools(include_worker_only=True, revealed=None)
    }


def _tool_names_in_governance() -> set[str]:
    return set(load_governance_config().tools)


# ── AC-1 ──────────────────────────────────────────────────────────────────────────


def test_every_real_skill_and_tool_name_states_an_objective() -> None:
    names = {*skill_names(), *_registered(), *_tool_names_in_governance()}
    assert name_violations(names) == ()


# ── AC-2 ──────────────────────────────────────────────────────────────────────────


def test_every_skill_and_skill_provided_tool_names_something_real() -> None:
    registry = get_default_registry()
    skill_provided = {
        t.name: t.description
        for t in registry.list_tools(include_worker_only=True, revealed=None)
        if t.skill_provided
    }
    assert set(skill_provided) == set(_TOOLS)
    descriptions = {name: doc.description for name, doc in get_all_skills().items()}
    descriptions.update(skill_provided)
    known = {*skill_names(), *_registered(), *_tool_names_in_governance()}
    assert not_for_violations(descriptions, known) == ()


# ── AC-4 ──────────────────────────────────────────────────────────────────────────


def test_each_tool_has_a_provider_and_no_third_name_is_provided() -> None:
    skills = get_all_skills()
    provided = {tool for doc in skills.values() for tool in doc.provides_tools}
    assert provided == set(_TOOLS)
    for tool in _TOOLS:
        assert providers_of(tool), f"{tool} has no providing skill"
    assert provider_violations(skills, _registered()) == ()


def test_the_providers_are_the_skills_that_the_adr_names() -> None:
    """ADR-0156 A4: the column "Tool that it reveals"."""
    assert providers_of("measure_model_latency") == ("model-latency",)
    assert providers_of("search_logs") == (
        "log-search",
        "service-health",
        "spend-and-tokens",
        "turn-explanation",
    )


def test_every_provider_is_in_the_primarys_catalog_and_reveals_its_tool() -> None:
    """The primary's catalog is every skill, so each provider can reveal its tool (A5.4)."""
    catalog = assemble_skill_catalog(None)
    for tool in _TOOLS:
        for name in providers_of(tool):
            assert f"- {name}:" in catalog, name
            assert tool in tools_revealed_by(name, held=None), name


# ── AC-3 ──────────────────────────────────────────────────────────────────────────


def _ctx(revealed: set[str]) -> ExecutionContext:
    ctx = ExecutionContext(
        session_id="session-fre1596",
        trace_id="trace-fre1596",
        user_message="How many errors did the agent log in the last hour?",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=[{"role": "user", "content": "How many errors did the agent log?"}],
    )
    ctx.turn_started_at = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    ctx.revealed_tools = set(revealed)
    return ctx


async def _primary_request_tools(revealed: set[str]) -> set[str]:
    """Run the primary's ``step_llm_call`` with the real registry and return the sent tools."""
    from personal_agent.llm_client.models import Dialect

    client = MagicMock()
    client.model_configs = {}
    client.dialect_for_role.return_value = Dialect.LLAMACPP_QWEN
    client.respond = AsyncMock(
        return_value={
            "content": "Done.",
            "tool_calls": [],
            "response_id": None,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )
    session = MagicMock()
    session.add_message = AsyncMock()
    session.get_messages = AsyncMock(return_value=[])
    registry = get_default_registry()
    with (
        patch.object(ex, "_tool_registry", registry),
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=client),
    ):
        await ex.step_llm_call(_ctx(revealed), session, TraceContext.new_trace())
    sent = client.respond.call_args.kwargs["tools"] or []
    return {t["function"]["name"] for t in sent}


@pytest.mark.asyncio
async def test_the_first_primary_request_holds_neither_tool() -> None:
    sent = await _primary_request_tools(set())
    assert sent, "the primary's request holds no tools at all: the capture is not a request"
    assert not set(_TOOLS) & sent


@pytest.mark.asyncio
async def test_the_primary_request_holds_the_tool_that_a_skill_revealed() -> None:
    sent = await _primary_request_tools({"search_logs"})
    assert "search_logs" in sent
    assert "measure_model_latency" not in sent


@pytest.mark.asyncio
async def test_a_general_worker_request_holds_both_tools() -> None:
    grant = evaluate_sub_agent_tool_grant(
        WORKER_TYPES[WorkerType.GENERAL].tools, Mode.NORMAL, load_governance_config()
    )
    spec = SubAgentSpec(
        task="Count the errors of the last hour.",
        context=[],
        timeout_seconds=30.0,
        tools=[t for t in grant.granted if t != "read"],
        worker_type=WorkerType.GENERAL,
    )
    with shared_layer(worker_layer()):
        run = await run_worker(spec, [])
    sent: list[dict[str, Any]] = run.calls[0]["tools"] or []
    assert set(_TOOLS) <= {t["function"]["name"] for t in sent}


@pytest.mark.asyncio
async def test_a_researcher_worker_request_holds_neither_tool() -> None:
    grant = evaluate_sub_agent_tool_grant(
        WORKER_TYPES[WorkerType.RESEARCHER].tools, Mode.NORMAL, load_governance_config()
    )
    spec = SubAgentSpec(
        task="Find the release date of a library.",
        context=[],
        timeout_seconds=30.0,
        tools=list(grant.granted),
        worker_type=WorkerType.RESEARCHER,
    )
    with shared_layer(worker_layer()):
        run = await run_worker(spec, [])
    sent: list[dict[str, Any]] = run.calls[0]["tools"] or []
    assert not set(_TOOLS) & {t["function"]["name"] for t in sent}
