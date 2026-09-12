"""ADR-0150 D1 (FRE-1494) — the constrained landing call and its validity table.

AC-1: a report that is data is accepted; a report that is not data is not.
AC-2: one seeded negative per predicate.
AC-3: the voluntary stop lands and keeps its gap.
AC-4: a dialect declared False reports as text.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.llm_client.models import Dialect
from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import SubAgentSpec
from personal_agent.orchestrator.worker_types import WorkerType

_MARKERS = ("ZORPTAL7", "QUIBBIX9")
_SOURCES = ("https://a.example/one", "https://b.example/two")
_ABSENT = "FROBNAZ3"


def _researcher_spec(**overrides: Any) -> SubAgentSpec:
    fields: dict[str, Any] = {
        "task": f"find {', '.join(_MARKERS)} and {_ABSENT}",
        "context": [],
        "max_tokens": 1024,
        "timeout_seconds": 30.0,
        "tools": ["web_search"],
        "worker_type": WorkerType.RESEARCHER,
        "thoroughness": "quick",
    }
    fields.update(overrides)
    return SubAgentSpec(**fields)


def _llm_response(
    content: str,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = "stop",
) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls or [],
        "usage": {},
        "response_id": None,
        "raw": {},
        "finish_reason": finish_reason,
    }


def _stub_tool_layer() -> MagicMock:
    layer = MagicMock()
    layer.registry.get_tool_definitions_for_llm.return_value = [
        {
            "type": "function",
            "function": {"name": "web_search", "description": "d", "parameters": {}},
        }
    ]
    return layer


def _stub_client(
    landing_response: Callable[[dict[str, Any]], dict[str, Any]],
    dialect: Dialect | None = Dialect.LLAMACPP_QWEN,
) -> AsyncMock:
    """A worker that replies DONE with no tool calls, then a scripted landing call."""
    calls: list[dict[str, Any]] = []

    async def _respond(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if len(calls) == 1:
            return _llm_response("DONE")
        return landing_response(kwargs)

    client = AsyncMock()
    client.respond = AsyncMock(side_effect=_respond)
    client.dialect_for_role = MagicMock(return_value=dialect)
    client.provider = "slm_local"
    client.recorded_calls = calls
    return client


def _patches() -> Any:
    return (
        patch(
            "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
            return_value=_stub_tool_layer(),
        ),
        patch("personal_agent.orchestrator.sub_agent.dispatch_tool_call", AsyncMock()),
    )


def _valid_report_json(
    findings: list[dict[str, str]] | None = None,
    gaps: list[dict[str, str]] | None = None,
    working_notes: str = "notes",
    tool_gap: str = "",
) -> str:
    return json.dumps(
        {
            "working_notes": working_notes,
            "findings": findings if findings is not None else [],
            "gaps": gaps if gaps is not None else [],
            "tool_gap": tool_gap,
        }
    )


def _fixture_a_json() -> str:
    findings = [
        {"claim": f"Found {m}", "source_url": s, "date_or_period": "", "why_it_matters": "relevant"}
        for m, s in zip(_MARKERS, _SOURCES, strict=True)
    ]
    gaps = [{"looked_for": _ABSENT, "where": "web_search"}]
    return _valid_report_json(findings=findings, gaps=gaps)


class TestAC1ValidityTable:
    """Fixtures A-E — each row of ADR-0150 D1's validity table."""

    @pytest.mark.asyncio
    async def test_fixture_a_valid_report_is_synthesized(self) -> None:
        def _landing(kwargs: dict[str, Any]) -> dict[str, Any]:
            assert kwargs.get("response_format") is not None
            assert kwargs["response_format"]["json_schema"]["strict"] is True
            assert kwargs.get("tools") is not None
            assert kwargs.get("tool_choice") == "none"
            return _llm_response(_fixture_a_json())

        client = _stub_client(_landing)
        p1, p2 = _patches()
        with p1, p2:
            result = await run_sub_agent(spec=_researcher_spec(), llm_client=client, trace_id="t")

        assert result.report_kind == "synthesized"
        assert result.success is True
        assert result.report_schema == "worker_report_v1"
        assert result.report is not None
        claims = {f.claim for f in result.report.findings}
        for marker in _MARKERS:
            assert any(marker in claim for claim in claims)
            assert marker in result.summary
        gap_names = {g.looked_for for g in result.report.gaps}
        assert _ABSENT in gap_names
        assert _ABSENT in result.summary
        assert result.findings_dropped_invalid_source == 0

    @pytest.mark.asyncio
    async def test_fixture_b_prose_is_a_ledger(self) -> None:
        """Fixture B: the stub returns prose regardless of response_format."""

        def _landing(kwargs: dict[str, Any]) -> dict[str, Any]:
            return _llm_response("Here is my answer in plain prose, not JSON at all.")

        client = _stub_client(_landing)
        p1, p2 = _patches()
        with p1, p2:
            result = await run_sub_agent(spec=_researcher_spec(), llm_client=client, trace_id="t")

        assert result.report_kind == "ledger"
        assert result.success is False
        assert result.report is None

    @pytest.mark.asyncio
    async def test_fixture_c_valid_but_empty_is_a_ledger(self) -> None:
        def _landing(kwargs: dict[str, Any]) -> dict[str, Any]:
            return _llm_response(_valid_report_json(working_notes=""))

        client = _stub_client(_landing)
        p1, p2 = _patches()
        with p1, p2:
            result = await run_sub_agent(spec=_researcher_spec(), llm_client=client, trace_id="t")

        assert result.report_kind == "ledger"
        assert result.success is False
        assert "no findings and no gaps" in result.summary
        assert result.report is None

    @pytest.mark.asyncio
    async def test_fixture_d_whitespace_only_fields_are_a_ledger(self) -> None:
        def _landing(kwargs: dict[str, Any]) -> dict[str, Any]:
            findings = [
                {"claim": " ", "source_url": " ", "date_or_period": " ", "why_it_matters": " "}
            ]
            return _llm_response(_valid_report_json(findings=findings))

        client = _stub_client(_landing)
        p1, p2 = _patches()
        with p1, p2:
            result = await run_sub_agent(spec=_researcher_spec(), llm_client=client, trace_id="t")

        assert result.report_kind == "ledger"
        assert result.success is False
        assert result.report is None

    @pytest.mark.asyncio
    async def test_fixture_e_invalid_source_is_dropped_and_counted(self) -> None:
        def _landing(kwargs: dict[str, Any]) -> dict[str, Any]:
            findings = [
                {
                    "claim": "an unsourced claim",
                    "source_url": "see above",
                    "date_or_period": "",
                    "why_it_matters": "matters",
                },
                {
                    "claim": "a sourced claim",
                    "source_url": "https://good.example/z",
                    "date_or_period": "",
                    "why_it_matters": "matters too",
                },
            ]
            return _llm_response(_valid_report_json(findings=findings))

        client = _stub_client(_landing)
        p1, p2 = _patches()
        with p1, p2:
            result = await run_sub_agent(spec=_researcher_spec(), llm_client=client, trace_id="t")

        assert result.report_kind == "synthesized"
        assert result.report is not None
        assert len(result.report.findings) == 1
        assert result.report.findings[0].source_url == "https://good.example/z"
        assert result.findings_dropped_invalid_source == 1


class TestAC2SeededNegatives:
    """Disable each mechanism in turn; the corresponding fixture must flip."""

    @pytest.mark.asyncio
    async def test_response_format_withheld_makes_fixture_a_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Isolates the request mechanism from dialect acceptance (AC-4's own
        row): the dialect still declares True, so the landing is still SCORED
        as schema-backed, but the wire request omits response_format. A's
        format-sensitive stub therefore returns prose, which fails to parse.
        """
        import personal_agent.orchestrator.sub_agent as sa

        monkeypatch.setattr(sa, "_landing_response_format", lambda: None)

        def _landing(kwargs: dict[str, Any]) -> dict[str, Any]:
            if kwargs.get("response_format") is not None:
                return _llm_response(_fixture_a_json())
            return _llm_response("plain prose report, no JSON")

        client = _stub_client(_landing)
        p1, p2 = _patches()
        with p1, p2:
            result = await run_sub_agent(spec=_researcher_spec(), llm_client=client, trace_id="t")

        landing_kwargs = client.recorded_calls[-1]
        assert landing_kwargs.get("response_format") is None
        assert result.report_kind == "ledger"
        assert result.report is None

    @pytest.mark.asyncio
    async def test_parsing_bypassed_makes_fixture_b_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import personal_agent.orchestrator.sub_agent as sa
        from personal_agent.orchestrator.worker_types import Finding, WorkerReport

        monkeypatch.setattr(
            sa,
            "_parse_and_validate_worker_report",
            lambda content: WorkerReport(
                working_notes="",
                findings=[
                    Finding(
                        claim="fabricated from non-JSON content",
                        source_url="https://x.example",
                        date_or_period="",
                        why_it_matters="stood in for the bypassed check",
                    )
                ],
                gaps=[],
                tool_gap="",
            ),
        )

        def _landing(kwargs: dict[str, Any]) -> dict[str, Any]:
            return _llm_response("Here is my answer in plain prose, not JSON at all.")

        client = _stub_client(_landing)
        p1, p2 = _patches()
        with p1, p2:
            result = await run_sub_agent(spec=_researcher_spec(), llm_client=client, trace_id="t")

        assert result.report_kind == "synthesized"

    @pytest.mark.asyncio
    async def test_non_emptiness_disabled_makes_fixture_c_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import personal_agent.orchestrator.sub_agent as sa

        monkeypatch.setattr(sa, "_worker_report_nonempty", lambda report: True)

        def _landing(kwargs: dict[str, Any]) -> dict[str, Any]:
            return _llm_response(_valid_report_json(working_notes=""))

        client = _stub_client(_landing)
        p1, p2 = _patches()
        with p1, p2:
            result = await run_sub_agent(spec=_researcher_spec(), llm_client=client, trace_id="t")

        assert result.report_kind == "synthesized"

    @pytest.mark.asyncio
    async def test_whitespace_check_disabled_makes_a_blank_finding_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Isolates the whitespace predicate from the independent source_url check.

        A finding whose ``source_url`` is a real URL but whose ``claim`` and
        ``why_it_matters`` are whitespace-only: with the mechanism live this is
        `ledger` (fixture D's own shape asserts the combined case); with the
        whitespace check disabled it must become `synthesized`, since nothing
        else in the table rejects it.
        """
        import personal_agent.orchestrator.sub_agent as sa

        blank_finding = {
            "claim": " ",
            "source_url": "https://good.example/z",
            "date_or_period": "",
            "why_it_matters": " ",
        }

        def _landing(kwargs: dict[str, Any]) -> dict[str, Any]:
            return _llm_response(_valid_report_json(findings=[blank_finding]))

        client = _stub_client(_landing)
        p1, p2 = _patches()
        with p1, p2:
            result = await run_sub_agent(spec=_researcher_spec(), llm_client=client, trace_id="t")
        assert result.report_kind == "ledger"

        monkeypatch.setattr(sa, "_worker_report_fields_nonblank", lambda report: True)
        client = _stub_client(_landing)
        with p1, p2:
            result = await run_sub_agent(spec=_researcher_spec(), llm_client=client, trace_id="t")
        assert result.report_kind == "synthesized"


class TestAC3VoluntaryStopLands:
    """The DONE reply is never the report; the landing call is, and the gap survives."""

    @pytest.mark.asyncio
    async def test_stop_reply_gap_carries_when_report_names_none(self) -> None:
        calls: list[dict[str, Any]] = []

        async def _respond(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            if len(calls) == 1:
                return _llm_response("DONE\nTOOL_GAP: fetch_url")
            return _llm_response(_fixture_a_json())

        client = AsyncMock()
        client.respond = AsyncMock(side_effect=_respond)
        client.dialect_for_role = MagicMock(return_value=Dialect.LLAMACPP_QWEN)
        client.provider = "slm_local"

        p1, p2 = _patches()
        with p1, p2:
            result = await run_sub_agent(spec=_researcher_spec(), llm_client=client, trace_id="t")

        assert len(calls) == 2
        landing_kwargs = calls[-1]
        assert landing_kwargs.get("response_format") is not None
        assert landing_kwargs.get("tool_choice") == "none"
        assert result.stop_reason == "completed"
        assert result.report_kind == "synthesized"
        assert result.stated_tool_gap == "fetch_url"
        appended = [m for m in landing_kwargs["messages"] if m.get("role") == "assistant"]
        assert appended, "the DONE reply was never appended as transcript notes"
        assert appended[-1]["content"] == "DONE"

    @pytest.mark.asyncio
    async def test_report_tool_gap_wins_over_the_stop_reply(self) -> None:
        calls: list[dict[str, Any]] = []

        async def _respond(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            if len(calls) == 1:
                return _llm_response("DONE\nTOOL_GAP: fetch_url")
            return _llm_response(_valid_report_json(tool_gap="run_python"))

        client = AsyncMock()
        client.respond = AsyncMock(side_effect=_respond)
        client.dialect_for_role = MagicMock(return_value=Dialect.LLAMACPP_QWEN)
        client.provider = "slm_local"

        p1, p2 = _patches()
        with p1, p2:
            result = await run_sub_agent(spec=_researcher_spec(), llm_client=client, trace_id="t")

        assert result.stated_tool_gap == "run_python"


class TestAC4FalseDialectReportsAsText:
    @pytest.mark.asyncio
    async def test_declared_false_sends_no_response_format(self) -> None:
        def _landing(kwargs: dict[str, Any]) -> dict[str, Any]:
            return _llm_response("A plain-text report, since this dialect is not constrained.")

        client = _stub_client(_landing, dialect=Dialect.ANTHROPIC_BUDGET)
        p1, p2 = _patches()
        with p1, p2:
            result = await run_sub_agent(spec=_researcher_spec(), llm_client=client, trace_id="t")

        landing_kwargs = client.recorded_calls[-1]
        assert landing_kwargs.get("response_format") is None
        assert result.report is None
        assert result.report_schema is None
        assert result.report_kind == "synthesized"
        assert result.success is True

    @pytest.mark.asyncio
    async def test_declared_false_logs_the_warning(self) -> None:
        import structlog.testing

        def _landing(kwargs: dict[str, Any]) -> dict[str, Any]:
            return _llm_response("prose report")

        client = _stub_client(_landing, dialect=Dialect.ANTHROPIC_ADAPTIVE)
        p1, p2 = _patches()
        with p1, p2, structlog.testing.capture_logs() as logs:
            await run_sub_agent(spec=_researcher_spec(), llm_client=client, trace_id="t")

        warnings = [e for e in logs if e.get("event") == "structured_landing_unsupported_declared"]
        assert len(warnings) == 1
