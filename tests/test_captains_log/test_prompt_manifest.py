"""Tests for the prompt-composition manifest builder (FRE-409)."""

from __future__ import annotations

from typing import Any

import pytest


def _model_call_event(
    callsite: str = "orchestrator.primary",
    component_ids: list[str] | None = None,
    static_prefix_hash: str = "abc123def456abcd",
    dynamic_hash: str = "1122334455667788",
    use_event_type_key: bool = False,
) -> dict[str, Any]:
    """Build a synthetic model_call_completed trace event.

    By default uses the "event" key that get_trace_events (local log source)
    returns.  Pass use_event_type_key=True to simulate ES-shaped events.
    """
    event_key = "event_type" if use_event_type_key else "event"
    return {
        event_key: "model_call_completed",
        "prompt_callsite": callsite,
        "prompt_component_ids": component_ids if component_ids is not None else ["tool_awareness", "skill_index"],
        "prompt_static_prefix_hash": static_prefix_hash,
        "prompt_dynamic_hash": dynamic_hash,
    }


class TestBuildPromptManifest:
    """build_prompt_manifest assembles a 3-line manifest string."""

    def test_returns_three_lines(self) -> None:
        from personal_agent.captains_log.prompt_manifest import build_prompt_manifest

        events = [_model_call_event()]
        result = build_prompt_manifest(events)
        lines = result.splitlines()
        assert len(lines) == 3

    def test_components_line_lists_active_components(self) -> None:
        from personal_agent.captains_log.prompt_manifest import build_prompt_manifest

        events = [_model_call_event(component_ids=["tool_awareness", "memory_section", "skill_index"])]
        result = build_prompt_manifest(events)
        first_line = result.splitlines()[0]
        assert first_line.startswith("Active prompt components: ")
        assert "tool_awareness" in first_line
        assert "memory_section" in first_line
        assert "skill_index" in first_line

    def test_components_line_preserves_order(self) -> None:
        from personal_agent.captains_log.prompt_manifest import build_prompt_manifest

        components = ["tool_awareness", "deployment_context", "operator_stanza", "skill_index"]
        events = [_model_call_event(component_ids=components)]
        result = build_prompt_manifest(events)
        first_line = result.splitlines()[0]
        # All must appear in the listed order
        positions = [first_line.index(c) for c in components]
        assert positions == sorted(positions)

    def test_static_prefix_hash_in_second_line(self) -> None:
        from personal_agent.captains_log.prompt_manifest import build_prompt_manifest

        events = [_model_call_event(static_prefix_hash="deadbeef12345678")]
        result = build_prompt_manifest(events)
        second_line = result.splitlines()[1]
        assert second_line == "Static prefix hash: deadbeef12345678"

    def test_no_components_shows_none(self) -> None:
        from personal_agent.captains_log.prompt_manifest import build_prompt_manifest

        events = [_model_call_event(component_ids=[])]
        result = build_prompt_manifest(events)
        first_line = result.splitlines()[0]
        assert "(none)" in first_line

    def test_quality_line_with_rating_lookup(self) -> None:
        from personal_agent.captains_log.prompt_manifest import build_prompt_manifest

        events = [_model_call_event()]
        result = build_prompt_manifest(
            events,
            mean_rating_lookup={"orchestrator.primary": (2.10, 43)},
        )
        third_line = result.splitlines()[2]
        assert "mean rating = 2.10" in third_line
        assert "n=43" in third_line
        assert "last 7 days" in third_line

    def test_quality_line_no_rating_for_callsite(self) -> None:
        from personal_agent.captains_log.prompt_manifest import build_prompt_manifest

        events = [_model_call_event()]
        result = build_prompt_manifest(events, mean_rating_lookup={})
        third_line = result.splitlines()[2]
        assert "no recent ratings" in third_line
        assert "last 7 days" in third_line

    def test_quality_line_no_lookup_at_all(self) -> None:
        from personal_agent.captains_log.prompt_manifest import build_prompt_manifest

        events = [_model_call_event()]
        result = build_prompt_manifest(events)
        third_line = result.splitlines()[2]
        assert "no recent ratings" in third_line

    def test_returns_unavailable_when_no_identity_event(self) -> None:
        from personal_agent.captains_log.prompt_manifest import build_prompt_manifest

        # Non-model events only
        events: list[dict[str, Any]] = [
            {"event_type": "request_start", "trace_id": "t"},
            {"event_type": "tool_call", "tool_name": "search"},
        ]
        result = build_prompt_manifest(events)
        assert result == "Prompt manifest: unavailable"

    def test_returns_unavailable_for_empty_events(self) -> None:
        from personal_agent.captains_log.prompt_manifest import build_prompt_manifest

        result = build_prompt_manifest([])
        assert result == "Prompt manifest: unavailable"

    def test_non_primary_callsite_used_as_fallback(self) -> None:
        """When no orchestrator.primary event exists, fall back to last identity event."""
        from personal_agent.captains_log.prompt_manifest import build_prompt_manifest

        events = [
            _model_call_event(
                callsite="gateway.chat",
                component_ids=["gateway_persona"],
                static_prefix_hash="cccc1111eeee2222",
            )
        ]
        result = build_prompt_manifest(events)
        lines = result.splitlines()
        assert len(lines) == 3
        assert "gateway_persona" in lines[0]
        assert "cccc1111eeee2222" in lines[1]
        assert "gateway.chat" in lines[2]

    def test_primary_callsite_preferred_over_others(self) -> None:
        """orchestrator.primary wins even if it appears before another callsite event."""
        from personal_agent.captains_log.prompt_manifest import build_prompt_manifest

        events = [
            _model_call_event(
                callsite="gateway.chat",
                component_ids=["gateway_persona"],
                static_prefix_hash="bbbbbbbbbbbbbbbb",
            ),
            _model_call_event(
                callsite="orchestrator.primary",
                component_ids=["tool_awareness", "skill_index"],
                static_prefix_hash="aaaaaaaaaaaaaaaa",
            ),
        ]
        result = build_prompt_manifest(events)
        lines = result.splitlines()
        assert "aaaaaaaaaaaaaaaa" in lines[1], "primary callsite hash should win"

    def test_identity_event_without_hash_skipped(self) -> None:
        """model_call_completed without prompt_static_prefix_hash is not treated as identity."""
        from personal_agent.captains_log.prompt_manifest import build_prompt_manifest

        # Use the real "event" key (log-file shape)
        events = [
            {"event": "model_call_completed", "prompt_callsite": "orchestrator.primary"},
        ]
        result = build_prompt_manifest(events)
        assert result == "Prompt manifest: unavailable"

    def test_event_type_key_also_accepted(self) -> None:
        """ES-shaped events with event_type= key (not event=) are also accepted."""
        from personal_agent.captains_log.prompt_manifest import build_prompt_manifest

        events = [_model_call_event(use_event_type_key=True)]
        result = build_prompt_manifest(events)
        # Should parse correctly — not "unavailable"
        assert result != "Prompt manifest: unavailable"
        assert "tool_awareness" in result

    def test_rating_precision_two_decimal_places(self) -> None:
        from personal_agent.captains_log.prompt_manifest import build_prompt_manifest

        events = [_model_call_event()]
        result = build_prompt_manifest(
            events, mean_rating_lookup={"orchestrator.primary": (1.73333, 9)}
        )
        third_line = result.splitlines()[2]
        assert "mean rating = 1.73" in third_line
        assert "n=9" in third_line

    def test_never_raises_on_malformed_events(self) -> None:
        from personal_agent.captains_log.prompt_manifest import build_prompt_manifest

        # Corrupt data should degrade gracefully
        events: list[Any] = [
            None,
            42,
            {"event_type": "model_call_completed", "prompt_static_prefix_hash": None, "prompt_component_ids": None},
        ]
        result = build_prompt_manifest(events)
        # Either unavailable or valid manifest; must not raise
        assert isinstance(result, str)


class TestFormatQualityLine:
    """_format_quality_line formats the callsite quality signal."""

    def test_with_rating_shows_mean_and_count(self) -> None:
        from personal_agent.captains_log.prompt_manifest import _format_quality_line

        line = _format_quality_line("orchestrator.primary", {"orchestrator.primary": (2.10, 43)})
        assert "orchestrator.primary" in line
        assert "mean rating = 2.10" in line
        assert "n=43" in line

    def test_without_callsite_key_shows_no_recent(self) -> None:
        from personal_agent.captains_log.prompt_manifest import _format_quality_line

        line = _format_quality_line("orchestrator.primary", {})
        assert "no recent ratings" in line

    def test_none_lookup_shows_no_recent(self) -> None:
        from personal_agent.captains_log.prompt_manifest import _format_quality_line

        line = _format_quality_line("orchestrator.primary", None)
        assert "no recent ratings" in line
