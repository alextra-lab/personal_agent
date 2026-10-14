"""FRE-1608: the reflection model reads inputs that production really carries.

The fixtures are real recorded shapes, not synthetic events:

- ``fre1608_traces.json`` — the six traces behind the canceled tickets FRE-1373,
  1378, 1404, 1412, 1418 and 1429. Event names and a fixed list of fields only.
  User text is absent and error text is redacted.
- ``production_event_fields.json`` — the keys each relevant event carried in
  production ``agent-logs-*`` (a 45-day sample).

Every check is on the rendered input. None of them calls a model (AC-5).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    CaptainLogStatus,
    ChangeCategory,
    ChangeScope,
    ProposalSource,
    ProposedChange,
)
from personal_agent.captains_log.prompt_manifest import build_prompt_manifest
from personal_agent.captains_log.reflection import (
    _apply_read_before_emit,
    _build_failure_path_input,
    _summarize_telemetry,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_TRACES: dict[str, list[dict[str, Any]]] = json.loads(
    (_FIXTURES / "fre1608_traces.json").read_text()
)
_PRODUCTION: dict[str, Any] = json.loads((_FIXTURES / "production_event_fields.json").read_text())[
    "events"
]

# Keys a builder may read on any event: the event name and trace id are produced by the
# ES-to-log-entry translation layer, and ``status`` is the one generic failure marker.
_GENERIC_KEYS = frozenset({"event", "event_type", "trace_id", "status"})

# Events that signal a failure or a degraded turn. Written out here, not imported, so a
# drift in the production list shows up as a test failure.
_FAILURE_EVENTS = frozenset(
    {
        "tool_call_failed",
        "model_call_error",
        "planner_failed",
        "fallback_planner_used",
        "graceful_degradation_triggered",
        "turn_degraded",
        "turn_wall_clock_budget_exceeded_mid_call",
        "turn_lifetime_cap_exceeded_mid_call",
    }
)

_CANCELED = ["FRE-1373", "FRE-1378", "FRE-1404", "FRE-1412", "FRE-1418", "FRE-1429"]
_WITH_FAILURE = ["FRE-1373", "FRE-1412"]
_CLEAN = ["FRE-1404", "FRE-1418", "FRE-1429"]


class _RecordingEvent(dict[str, Any]):
    """An event dict that records every key a builder reads from it."""

    reads: set[tuple[str, str]]

    def __init__(self, data: Mapping[str, Any], reads: set[tuple[str, str]]) -> None:
        super().__init__(data)
        self.reads = reads

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        self.reads.add((str(dict.get(self, "event", "")), key))
        return super().get(key, default)

    def __getitem__(self, key: str) -> Any:
        self.reads.add((str(dict.get(self, "event", "")), key))
        return super().__getitem__(key)


def _recorded(trace: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], set[Any]]:
    reads: set[tuple[str, str]] = set()
    return [_RecordingEvent(e, reads) for e in trace], reads


def _unreal_reads(reads: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return reads of a key that production does not carry on that event."""
    bad: list[tuple[str, str]] = []
    for event, key in sorted(reads):
        if key in _GENERIC_KEYS or event not in _PRODUCTION:
            continue
        if key not in _PRODUCTION[event]["fields"]:
            bad.append((event, key))
    return bad


def _render(ticket: str) -> tuple[str, str, bool, str]:
    trace = _TRACES[ticket]
    return (
        _summarize_telemetry(trace, None),
        build_prompt_manifest(trace),
        *_build_failure_path_input(trace),
    )


def _count(trace: Sequence[Mapping[str, Any]], event: str) -> int:
    return sum(1 for e in trace if e["event"] == event)


class TestEveryInputIsReal:
    """AC-1 — each rendered line comes from an event and field production carries."""

    @pytest.mark.parametrize("ticket", _CANCELED)
    def test_builders_read_only_fields_production_carries(self, ticket: str) -> None:
        events, reads = _recorded(_TRACES[ticket])
        _summarize_telemetry(events, None)
        build_prompt_manifest(events)
        _build_failure_path_input(events)

        assert reads, "nothing was read, so the test proves nothing"
        assert _unreal_reads(reads) == []

    def test_the_guard_catches_the_original_defects(self) -> None:
        """Seeded negative: the reads FRE-1608 removed must be flagged."""
        original = {
            ("model_call_completed", "duration_ms"),
            ("tool_call_failed", "tool"),
            ("tool_call_failed", "error_message"),
            ("model_call_completed", "prompt_static_prefix_hash"),
        }
        assert _unreal_reads(original) == [
            ("model_call_completed", "duration_ms"),
            ("tool_call_failed", "error_message"),
            ("tool_call_failed", "tool"),
        ]

    def test_the_old_tool_event_does_not_exist_in_production(self) -> None:
        assert "tool_executed" not in _PRODUCTION
        assert "tool_call_completed" in _PRODUCTION

    def test_tool_counts_names_and_failures_render(self) -> None:
        """The tool lines that never rendered before now carry the recorded values."""
        trace = _TRACES["FRE-1378"]
        summary, *_ = _render("FRE-1378")
        names = {e["tool_name"] for e in trace if e["event"] == "tool_call_completed"}

        assert f"**Tools Used**: {_count(trace, 'tool_call_completed')} calls" in summary
        assert all(name in summary for name in names)
        assert f"**LLM Calls**: {_count(trace, 'model_call_completed')} calls" in summary

    def test_a_failed_tool_call_renders_its_tool_name(self) -> None:
        trace = _TRACES["FRE-1373"]
        summary, _, had_errors, excerpt = _render("FRE-1373")
        failed = next(e for e in trace if e["event"] == "tool_call_failed")

        assert f"**Tool Failures**: {_count(trace, 'tool_call_failed')} failures" in summary
        assert failed["tool_name"] in summary
        assert had_errors
        assert failed["tool_name"] in json.loads(excerpt)["failed_tool_calls"][0]["name"]

    def test_durations_say_not_available_never_zero(self) -> None:
        summary, *_ = _render("FRE-1378")

        assert "0ms" not in summary
        assert "duration: not available" in summary

    def test_missing_component_ids_are_reported_as_not_recorded(self) -> None:
        """Defect 7: an empty list means "no named composition", not "no components"."""
        event = {
            "event": "model_call_completed",
            "prompt_callsite": "role.sub_agent",
            "prompt_component_ids": [],
            "prompt_static_prefix_hash": "abc123",
        }

        manifest = build_prompt_manifest([event])

        assert "(none)" not in manifest
        assert "not recorded for callsite role.sub_agent" in manifest


class TestTheCanceledPremisesAreGone:
    """AC-2 — the six canceled tickets' traces, replayed through the input builders."""

    @pytest.mark.parametrize("ticket", _CANCELED)
    def test_no_false_premise_renders(self, ticket: str) -> None:
        summary, manifest, _, _ = _render(ticket)

        assert "0ms" not in summary
        assert "mean rating" not in manifest
        assert "2.00" not in manifest
        assert "2.00" not in summary

    @pytest.mark.parametrize("ticket", _CANCELED)
    def test_had_errors_follows_the_failure_events(self, ticket: str) -> None:
        trace = _TRACES[ticket]
        holds_failure = any(e["event"] in _FAILURE_EVENTS for e in trace)

        _, _, had_errors, excerpt = _render(ticket)

        assert had_errors is holds_failure
        assert bool(excerpt) is holds_failure

    @pytest.mark.parametrize("ticket", _WITH_FAILURE)
    def test_the_excerpt_names_the_model_and_planner_failures(self, ticket: str) -> None:
        _, _, _, excerpt = _render(ticket)
        degradation = json.loads(excerpt)["degradation_events"]

        assert any(d.startswith("model_call_error") for d in degradation)
        assert any(d.startswith("planner_failed") for d in degradation)
        assert any(d.startswith("graceful_degradation_triggered") for d in degradation)

    @pytest.mark.parametrize("ticket", _WITH_FAILURE)
    def test_the_summary_lists_errors_with_their_text(self, ticket: str) -> None:
        summary, *_ = _render(ticket)

        assert "**Errors**" in summary
        assert "model_call_error" in summary
        assert "Unknown error" not in summary

    def test_a_failed_subagent_is_not_called_a_tool(self) -> None:
        """``subagent_completed status=failed`` used to render as a tool named "unknown"."""
        _, _, _, excerpt = _render("FRE-1373")
        payload = json.loads(excerpt)

        assert all(call["name"] != "unknown" for call in payload["failed_tool_calls"])
        assert any(d.startswith("subagent_completed") for d in payload["degradation_events"])

    def test_the_prompt_manifest_has_no_rating_line(self) -> None:
        manifest = build_prompt_manifest(_TRACES["FRE-1418"])

        assert manifest.splitlines()[0].startswith("Active prompt components:")
        assert len(manifest.splitlines()) == 2


class TestACleanTraceRendersNoError:
    """AC-3 — the seeded negative: a trace with no failure event reports none."""

    @pytest.mark.parametrize("ticket", _CLEAN)
    def test_precondition_the_trace_holds_no_failure(self, ticket: str) -> None:
        assert not any(e["event"] in _FAILURE_EVENTS for e in _TRACES[ticket])
        assert not any(e.get("success") is False for e in _TRACES[ticket])

    @pytest.mark.parametrize("ticket", _CLEAN)
    def test_had_errors_false_and_no_excerpt(self, ticket: str) -> None:
        summary, _, had_errors, excerpt = _render(ticket)

        assert had_errors is False
        assert excerpt == ""
        assert "**Errors**" not in summary
        assert "**Tool Failures**" not in summary
        assert "**Degraded**" not in summary


class TestFailureEventShapes:
    """The excerpt covers each failure class the ticket names, once per call."""

    @staticmethod
    def _excerpt(*events: Mapping[str, Any]) -> dict[str, Any]:
        had_errors, payload = _build_failure_path_input(list(events))
        assert had_errors
        return json.loads(payload)

    def test_a_refused_call_logged_only_as_completed_failure_is_counted(self) -> None:
        """ADR-0156 D6.3: a call refused before it ran logs ``tool_call_completed`` only."""
        payload = self._excerpt(
            {
                "event": "tool_call_completed",
                "tool_name": "fetch_url",
                "success": False,
                "error_code": "unknown_tool",
                "executed": False,
            }
        )

        assert [c["name"] for c in payload["failed_tool_calls"]] == ["fetch_url"]

    def test_a_call_that_logs_both_events_is_counted_once(self) -> None:
        payload = self._excerpt(
            {"event": "tool_call_failed", "tool_name": "fetch_url", "error": "boom"},
            {"event": "tool_call_completed", "tool_name": "fetch_url", "success": False},
        )
        summary = _summarize_telemetry(
            [
                {"event": "tool_call_failed", "tool_name": "fetch_url", "error": "boom"},
                {"event": "tool_call_completed", "tool_name": "fetch_url", "success": False},
            ],
            None,
        )

        assert len(payload["failed_tool_calls"]) == 1
        assert payload["failed_tool_calls"][0]["error_message"] == "boom"
        assert "**Tool Failures**: 1 failures - fetch_url" in summary
        assert "**Tools Used**: 1 calls" in summary

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            ({"event": "model_call_error", "error": "x", "error_type": "LLMTimeout"}, "LLMTimeout"),
            ({"event": "planner_failed", "reason": "timeout"}, "timeout"),
            ({"event": "graceful_degradation_triggered", "reason": "budget"}, "budget"),
            ({"event": "turn_degraded", "reason": "budget"}, "budget"),
            ({"event": "expansion_controller_complete", "degraded": True}, "degraded"),
        ],
    )
    def test_degradation_events_are_covered(self, event: dict[str, Any], expected: str) -> None:
        payload = self._excerpt(event)

        assert payload["failed_tool_calls"] == []
        assert expected in payload["degradation_events"][0]

    def test_a_failed_calls_own_mirror_is_not_a_retry(self) -> None:
        """The mirrored ``tool_call_completed`` is the same failure, not a second call."""
        payload = self._excerpt(
            {"event": "tool_call_failed", "tool_name": "fetch_url", "error": "timeout"},
            {"event": "tool_call_completed", "tool_name": "fetch_url", "success": False},
        )

        assert payload["recovery_actions"] == []

    def test_a_real_retry_and_a_real_switch_are_reported(self) -> None:
        retry = self._excerpt(
            {"event": "tool_call_failed", "tool_name": "fetch_url", "error": "timeout"},
            {"event": "tool_call_completed", "tool_name": "fetch_url", "success": False},
            {"event": "tool_call_completed", "tool_name": "fetch_url", "success": True},
        )
        switch = self._excerpt(
            {"event": "tool_call_failed", "tool_name": "fetch_url", "error": "timeout"},
            {"event": "tool_call_completed", "tool_name": "web_search", "success": True},
        )

        assert retry["recovery_actions"] == ["Retried fetch_url with same arguments"]
        assert switch["recovery_actions"] == ["Switched from fetch_url to web_search"]

    def test_an_undegraded_expansion_is_not_a_failure(self) -> None:
        had_errors, payload = _build_failure_path_input(
            [{"event": "expansion_controller_complete", "degraded": False}]
        )

        assert had_errors is False
        assert payload == ""


def _entry_with_proposal() -> CaptainLogEntry:
    return CaptainLogEntry(
        entry_id="CL-fre1608",
        timestamp=datetime.now(timezone.utc),
        type=CaptainLogEntryType.REFLECTION,
        title="Task: test",
        rationale="because",
        proposed_change=ProposedChange(
            what="Add retry logic",
            why="Improves reliability",
            how="Wrap calls in tenacity",
            category=ChangeCategory.RELIABILITY,
            scope=ChangeScope.LLM_CLIENT,
            source=ProposalSource.REFLECTION,
            fingerprint="fp-fre1608",
            first_seen=datetime.now(timezone.utc),
        ),
        status=CaptainLogStatus.AWAITING_APPROVAL,
        telemetry_refs=[],
    )


class _StubSysgraphRepo:
    """A repository that records each write it is asked to make.

    ``read_before_emit`` increments ``seen_count`` or inserts a row on every branch, so a
    call here is a production write. No test touches a real sysgraph.
    """

    pool = object()

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, str | None]] = []

    async def read_before_emit(
        self, source: str, category: str, scope: str | None, proposal: Any
    ) -> Any:
        self.writes.append((source, category, scope))
        return SimpleNamespace(
            decision="reinforced",
            proposal_id=uuid4(),
            seen_count=7,
            fingerprint="fp-fre1608",
            first_seen=None,
        )


class TestEvalReflectionLeavesSysgraphAlone:
    """AC-4 — an eval reflection changes no production sysgraph row."""

    @pytest.mark.asyncio
    async def test_eval_reflection_makes_no_sysgraph_write(self) -> None:
        repo = _StubSysgraphRepo()
        entry = _entry_with_proposal()

        await _apply_read_before_emit(
            entry,
            repo,  # type: ignore[arg-type]
            trace_id="t-eval",
            eval_mode=True,
        )

        assert repo.writes == []
        assert entry.proposed_change is not None

    @pytest.mark.asyncio
    async def test_production_reflection_still_reads_and_writes(self) -> None:
        """Seeded positive: the same call without ``eval_mode`` reaches sysgraph."""
        repo = _StubSysgraphRepo()

        await _apply_read_before_emit(
            _entry_with_proposal(),
            repo,  # type: ignore[arg-type]
            trace_id="t-prod",
            eval_mode=False,
        )

        assert len(repo.writes) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dspy_available", [True, False])
    async def test_generate_reflection_entry_threads_eval_mode(self, dspy_available: bool) -> None:
        """Both generation paths (DSPy and the manual fallback) honour ``eval_mode``."""
        from personal_agent.captains_log import reflection

        repo = _StubSysgraphRepo()
        manual_reply = json.dumps(
            {
                "rationale": "r",
                "proposed_change": {
                    "what": "Add retry logic",
                    "why": "w",
                    "how": "h",
                    "category": "reliability",
                    "scope": "llm_client",
                },
                "supporting_metrics": [],
                "impact_assessment": None,
            }
        )
        client = SimpleNamespace(respond=AsyncMock(return_value={"content": manual_reply}))

        async def _to_thread(fn: Any, **kwargs: Any) -> Any:
            return fn(**kwargs)

        def _dspy(**_: Any) -> tuple[CaptainLogEntry, list[str]]:
            return _entry_with_proposal(), []

        model_def = SimpleNamespace(provider="local", id="m", max_tokens=100)
        with ExitStack() as stack:
            for p in (
                patch.object(reflection, "_fetch_trace_events", AsyncMock(return_value=[])),
                patch.object(reflection, "DSPY_AVAILABLE", dspy_available),
                patch.object(reflection, "generate_reflection_dspy", new=_dspy),
                patch.object(reflection.asyncio, "to_thread", new=_to_thread),
                patch.object(
                    reflection, "resolve_dspy_target", return_value=("k", model_def, False)
                ),
                patch.object(reflection, "get_llm_client_for_key", return_value=client),
            ):
                stack.enter_context(p)
            entry = await reflection.generate_reflection_entry(
                user_message="hi",
                trace_id="t-eval",
                steps_count=1,
                final_state="COMPLETED",
                reply_length=5,
                eval_mode=True,
                sysgraph_repo=repo,  # type: ignore[arg-type]
            )

        assert entry.eval_mode is True
        assert entry.proposed_change is not None, "the test never reached a proposal"
        assert repo.writes == []


def test_fixture_traces_carry_no_free_text() -> None:
    """Shape only: the fixtures hold no user text and no error text."""
    allowed_strings = {"event", "trace_id", "tool_name", "error_type", "reason", "status", "role"}
    for events in _TRACES.values():
        for event in events:
            assert event.get("error", "<redacted>") == "<redacted>"
            for key, value in event.items():
                if isinstance(value, str) and key not in allowed_strings:
                    assert key in {"error", "prompt_callsite", "prompt_static_prefix_hash"}, key
            assert str(event["trace_id"]).startswith("fixture-")
