"""FRE-1569 — a worker cannot take a full-size ``fetch_url`` page.

FRE-1564 gave the ``researcher`` worker ``fetch_url``. The tool returns up to 50,000
characters when asked, about 19,000 prompt tokens a page, and a researcher that fetched
seven of them stopped on ``context_reserve`` (trace ``b791f193``).

Every test here runs the shipped governance config and the real ``fetch_url_executor``.
Only the HTTP transport is a stub. A mock that returned a canned dict could not fail when
the ceiling was missing, so it would prove nothing.

AC-1: a sub-agent that asks for 50,000 characters receives at most the ceiling, and the
    clamp event fires.
AC-2: the primary still receives up to the tool's own cap, and no clamp event fires.
AC-3: a researcher run through seven large pages ends ``completed``, not ``context_reserve``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.testing

from personal_agent.config import settings
from personal_agent.config.governance_loader import load_governance_config
from personal_agent.governance.models import GovernanceConfig, SubAgentToolDecision
from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import SubAgentSpec
from personal_agent.orchestrator.tool_dispatch import dispatch_tool_call
from personal_agent.orchestrator.worker_types import WORKER_TYPES, WorkerType
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.fetch import _MAX_CHARS_CAP, fetch_url_executor, fetch_url_tool
from personal_agent.tools.schema_validator import validate_tool_arguments
from personal_agent.tools.types import ToolResult

#: A page longer than the tool's own 50,000-character cap, so the cap is what bounds it.
_PAGE_CHARS = 60_000

#: Prompt characters per token measured on the two failing traces: 50,000 characters of page
#: text added about 19,000 prompt tokens (ticket FRE-1569).
_CHARS_PER_TOKEN = 2.6

_CLAMP_EVENT = "sub_agent_tool_param_clamped"


def _page_html() -> str:
    body = " ".join(f"Sentence {i} of the fetched page." for i in range(4_000))
    return f"<html><body><p>{body}</p></body></html>"[:_PAGE_CHARS]


def _stub_http_client() -> AsyncMock:
    response = MagicMock()
    response.status_code = 200
    response.is_error = False
    response.text = _page_html()
    response.headers = {"content-type": "text/html; charset=utf-8"}
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _fetch_tool_layer(config: GovernanceConfig) -> MagicMock:
    """A tool layer that runs the real ``fetch_url_executor`` over a stub transport.

    Like ``ToolExecutionLayer.execute_tool``, it drops unknown parameters and validates the rest
    against the tool's own JSON Schema first. ``max_chars`` is declared a number, so a string
    never reaches the executor.
    """
    layer = MagicMock()
    layer.governance_config = config
    layer.registry.get_tool = MagicMock(return_value=None)
    layer.registry.get_tool_definitions_for_llm.return_value = [
        {"type": "function", "function": {"name": name, "description": "d", "parameters": {}}}
        for name in WORKER_TYPES[WorkerType.RESEARCHER].tools
    ]

    async def _execute(
        tool_name: str, arguments: dict[str, Any], ctx: TraceContext, **_: Any
    ) -> ToolResult:
        known = {p.name for p in fetch_url_tool.parameters}
        filtered = {k: v for k, v in arguments.items() if k in known}
        if errors := validate_tool_arguments(fetch_url_tool, filtered):
            return ToolResult(
                tool_name=tool_name,
                success=False,
                output={},
                error="; ".join(errors),
                latency_ms=0.0,
            )
        # The novelty tracker keeps a cwd-relative cache file; a test must not write to it.
        tracker = MagicMock()
        tracker.check_and_record = AsyncMock(
            return_value=SimpleNamespace(novel=False, registrable_domain="example.com")
        )
        with (
            patch(
                "personal_agent.tools.fetch.create_guarded_http_client",
                return_value=_stub_http_client(),
            ),
            patch("personal_agent.tools.fetch.get_novelty_tracker", return_value=tracker),
        ):
            output = await fetch_url_executor(ctx=ctx, **filtered)
        return ToolResult(
            tool_name=tool_name, success=True, output=output, error=None, latency_ms=1.0
        )

    layer.execute_tool = AsyncMock(side_effect=_execute)
    return layer


def _config_without_the_ceiling() -> GovernanceConfig:
    """The shipped config with ``fetch_url``'s ceiling removed — the seeded negative."""
    shipped = load_governance_config()
    decisions = dict(shipped.sub_agent_tools)
    decisions["fetch_url"] = SubAgentToolDecision(granted=True, reason="no ceiling, for the test")
    return shipped.model_copy(update={"sub_agent_tools": decisions})


_OMITTED = object()


async def _dispatch_fetch(
    layer: MagicMock, max_chars: object, *, principal: str | None
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {} if principal is None else {"principal": principal}
    arguments: dict[str, Any] = {"url": "https://example.com/page"}
    if max_chars is not _OMITTED:
        arguments["max_chars"] = max_chars
    return await dispatch_tool_call(
        tool_call_id="tc-1",
        tool_name="fetch_url",
        arguments=arguments,
        tool_layer=layer,
        trace_ctx=TraceContext(trace_id="t-1", session_id="s-1"),
        trace_id="t-1",
        session_id="s-1",
        loaded_skills=set(),
        **kwargs,
    )


def _ceiling() -> int:
    return load_governance_config().sub_agent_tools["fetch_url"].param_ceilings["max_chars"]


class TestAWorkerCannotTakeAFullSizePage:
    """AC-1 and AC-2 at the dispatch boundary, over the shipped config."""

    @pytest.mark.asyncio
    async def test_a_sub_agent_asking_for_the_maximum_receives_the_ceiling(self) -> None:
        layer = _fetch_tool_layer(load_governance_config())

        with structlog.testing.capture_logs() as logs:
            result = await _dispatch_fetch(layer, _MAX_CHARS_CAP, principal="sub_agent")

        assert result["success"] is True
        assert result["tool_layer_output"]["char_count"] == _ceiling()
        assert result["tool_layer_output"]["truncated"] is True
        clamps = [e for e in logs if e.get("event") == _CLAMP_EVENT]
        assert len(clamps) == 1
        assert clamps[0]["param"] == "max_chars"
        assert clamps[0]["requested"] == _MAX_CHARS_CAP
        assert clamps[0]["applied"] == _ceiling()

    @pytest.mark.asyncio
    async def test_a_sub_agent_sending_the_maximum_as_a_string_receives_no_page(self) -> None:
        """A numeric string is not a way around the ceiling: the tool's schema refuses it."""
        layer = _fetch_tool_layer(load_governance_config())

        result = await _dispatch_fetch(layer, str(_MAX_CHARS_CAP), principal="sub_agent")

        assert result["success"] is False
        assert result["tool_layer_output"] is None or not result["tool_layer_output"]

    @pytest.mark.asyncio
    async def test_a_sub_agent_that_omits_max_chars_stays_within_the_ceiling(self) -> None:
        """The tool's own default is the bound when nothing is sent, so it must not exceed it."""
        layer = _fetch_tool_layer(load_governance_config())

        result = await _dispatch_fetch(layer, _OMITTED, principal="sub_agent")

        assert result["success"] is True
        assert result["tool_layer_output"]["char_count"] <= _ceiling()

    @pytest.mark.asyncio
    async def test_a_sub_agent_asking_for_less_than_the_ceiling_is_not_clamped(self) -> None:
        """Seeded negative: an in-bounds request is served as asked, with no event."""
        layer = _fetch_tool_layer(load_governance_config())

        with structlog.testing.capture_logs() as logs:
            result = await _dispatch_fetch(layer, 3_000, principal="sub_agent")

        assert result["tool_layer_output"]["char_count"] == 3_000
        assert [e for e in logs if e.get("event") == _CLAMP_EVENT] == []

    @pytest.mark.asyncio
    async def test_the_primary_still_receives_up_to_the_tool_cap(self) -> None:
        """AC-2 — the primary's own limit is unchanged, and the clamp never fires for it."""
        layer = _fetch_tool_layer(load_governance_config())

        with structlog.testing.capture_logs() as logs:
            result = await _dispatch_fetch(layer, _MAX_CHARS_CAP, principal=None)

        assert result["tool_layer_output"]["char_count"] == _MAX_CHARS_CAP
        assert [e for e in logs if e.get("event") == _CLAMP_EVENT] == []

    @pytest.mark.asyncio
    async def test_without_the_ceiling_a_sub_agent_takes_the_full_page(self) -> None:
        """The instrument can fail: the same call, minus the ceiling, returns 50,000."""
        layer = _fetch_tool_layer(_config_without_the_ceiling())

        result = await _dispatch_fetch(layer, _MAX_CHARS_CAP, principal="sub_agent")

        assert result["tool_layer_output"]["char_count"] == _MAX_CHARS_CAP


def _researcher_spec() -> SubAgentSpec:
    return SubAgentSpec(
        task="find the opening hours of the museum",
        context=[],
        max_tokens=1024,
        timeout_seconds=30.0,
        tools=list(WORKER_TYPES[WorkerType.RESEARCHER].tools),
        worker_type=WorkerType.RESEARCHER,
        thoroughness="standard",
    )


def _report_json() -> str:
    return json.dumps({"working_notes": "n", "findings": [], "gaps": [], "tool_gap": ""})


class _PageHungryResearcher:
    """A model that fetches ``pages`` pages at ``max_chars`` 50,000, then stops.

    Its reported ``usage.prompt_tokens`` follows the ratio measured on the failing traces, so
    the loop's own context reserve sees the pressure the real model server reported.
    """

    def __init__(self, pages: int) -> None:
        self.pages = pages
        self.fetches = 0
        self.peak_prompt_tokens = 0
        self.model_def = SimpleNamespace(context_length=131072, max_tokens=8192)
        self.provider = "slm_local"
        self.dialect_for_role = MagicMock(return_value=None)
        self.respond = AsyncMock(side_effect=self._respond)

    async def _respond(self, **kwargs: Any) -> dict[str, Any]:
        chars = sum(
            len(str(m.get("content") or "")) + len(str(m.get("tool_calls") or ""))
            for m in kwargs["messages"]
        )
        prompt_tokens = round(chars / _CHARS_PER_TOKEN)
        self.peak_prompt_tokens = max(self.peak_prompt_tokens, prompt_tokens)
        base: dict[str, Any] = {
            "role": "assistant",
            "tool_calls": [],
            "usage": {"prompt_tokens": prompt_tokens},
            "response_id": None,
            "raw": {},
            "finish_reason": "stop",
        }
        if kwargs.get("response_format") is not None or kwargs.get("tool_choice") == "none":
            return {**base, "content": _report_json()}
        if self.fetches < self.pages:
            self.fetches += 1
            call = {
                "id": f"call-{self.fetches}",
                "name": "fetch_url",
                "arguments": json.dumps(
                    {"url": f"https://example.com/p{self.fetches}", "max_chars": _MAX_CHARS_CAP}
                ),
            }
            return {**base, "content": "", "tool_calls": [call], "finish_reason": "tool_calls"}
        return {**base, "content": "DONE"}


class TestAMultiPageResearcherFinishes:
    """AC-3 — seven large pages through the real clamp, the real executor and the real loop."""

    @pytest.mark.asyncio
    async def test_a_researcher_that_fetches_seven_large_pages_completes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 20)
        model = _PageHungryResearcher(pages=7)
        layer = _fetch_tool_layer(load_governance_config())

        with patch(
            "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
            return_value=layer,
        ):
            result = await run_sub_agent(
                spec=_researcher_spec(), llm_client=model, trace_id="t", session_id="s"
            )

        assert model.fetches == 7
        assert result.stop_reason == "completed"
        reserve_line = model.model_def.context_length - settings.sub_agent_context_reserve_tokens
        assert model.peak_prompt_tokens < reserve_line / 2

    @pytest.mark.asyncio
    async def test_without_the_ceiling_the_same_researcher_stops_on_context_reserve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The instrument can fail: the failure in the ticket reproduces when the ceiling is gone."""
        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 20)
        model = _PageHungryResearcher(pages=7)
        layer = _fetch_tool_layer(_config_without_the_ceiling())

        with patch(
            "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
            return_value=layer,
        ):
            result = await run_sub_agent(
                spec=_researcher_spec(), llm_client=model, trace_id="t", session_id="s"
            )

        assert result.stop_reason == "context_reserve"
