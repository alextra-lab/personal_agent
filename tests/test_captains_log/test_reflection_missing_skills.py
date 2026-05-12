"""Tests for reflection-time capability-gap capture (FRE-328 follow-up).

Verifies that `_emit_missing_skill_events`:

- emits one `missing_skill_requested` log event per accepted name,
- rejects malformed names without emitting,
- dedupes names within a single call,
- caps at 3 emitted names per reflection.

These events flow into the same Elasticsearch bucket scanned by
`InsightsEngine.detect_missing_skill_patterns`, so reflection-time gap
recognition reuses the FRE-328 aggregation → Linear pipeline.
"""

from typing import Any

import pytest

from personal_agent.captains_log import reflection_dspy


def _capture_warnings(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Replace `reflection_dspy.log.warning` with a recorder and return the buffer."""
    captured: list[tuple[str, dict[str, Any]]] = []

    def fake_warning(event: str, **kwargs: Any) -> None:
        captured.append((event, kwargs))

    monkeypatch.setattr(reflection_dspy.log, "warning", fake_warning)
    return captured


class TestEmitMissingSkillEvents:
    """Behavioral contract for `_emit_missing_skill_events`."""

    def test_empty_input_emits_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No string → no events."""
        captured = _capture_warnings(monkeypatch)
        result = reflection_dspy._emit_missing_skill_events("", trace_id="t-1")
        assert result == []
        assert captured == []

    def test_whitespace_only_emits_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Whitespace string → no events."""
        captured = _capture_warnings(monkeypatch)
        result = reflection_dspy._emit_missing_skill_events("   ,  ,  ", trace_id="t-1")
        assert result == []
        assert captured == []

    def test_single_valid_name_emits_one_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One valid name → one structured event."""
        captured = _capture_warnings(monkeypatch)
        result = reflection_dspy._emit_missing_skill_events("slack-notify", trace_id="trace-abc")
        assert result == ["slack-notify"]
        assert len(captured) == 1
        event, kw = captured[0]
        assert event == "missing_skill_requested"
        assert kw["requested_name"] == "slack-notify"
        assert kw["source"] == "reflection"
        assert kw["trace_id"] == "trace-abc"

    def test_multiple_names_emit_in_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Comma-separated names → one event each, preserved order."""
        captured = _capture_warnings(monkeypatch)
        result = reflection_dspy._emit_missing_skill_events(
            "slack-notify, pagerduty-alert, github-release",
            trace_id="t-multi",
        )
        assert result == ["slack-notify", "pagerduty-alert", "github-release"]
        names = [kw["requested_name"] for _, kw in captured]
        assert names == result

    def test_dedup_within_single_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same name twice in one reflection → emitted once only."""
        captured = _capture_warnings(monkeypatch)
        result = reflection_dspy._emit_missing_skill_events(
            "slack-notify, slack-notify, Slack-Notify",
            trace_id="t-dup",
        )
        assert result == ["slack-notify"]
        assert len(captured) == 1

    def test_invalid_names_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Names with spaces, uppercase mid-word, or special chars are silently skipped."""
        captured = _capture_warnings(monkeypatch)
        # Mixed: one valid, two invalid (space and punctuation).
        result = reflection_dspy._emit_missing_skill_events(
            "ok-name, bad name, bad_name!",
            trace_id="t-mix",
        )
        assert result == ["ok-name"]
        assert len(captured) == 1
        assert captured[0][1]["requested_name"] == "ok-name"

    def test_cap_at_three_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Even if the LLM returns 5 names, only the first 3 are emitted."""
        captured = _capture_warnings(monkeypatch)
        result = reflection_dspy._emit_missing_skill_events(
            "skill-a, skill-b, skill-c, skill-d, skill-e",
            trace_id="t-cap",
        )
        assert result == ["skill-a", "skill-b", "skill-c"]
        assert len(captured) == 3

    def test_lowercase_normalization(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Names are lowercased so dedup is case-insensitive across runs."""
        captured = _capture_warnings(monkeypatch)
        result = reflection_dspy._emit_missing_skill_events("Slack-Notify", trace_id="t-case")
        assert result == ["slack-notify"]
        assert captured[0][1]["requested_name"] == "slack-notify"


class TestDspySignatureField:
    """The `missing_skill_names` field must be part of the DSPy reflection contract."""

    def test_signature_declares_field_when_dspy_available(self) -> None:
        """When dspy is installed, the GenerateReflection class exposes the new output field."""
        if not reflection_dspy.DSPY_AVAILABLE:
            pytest.skip("dspy not installed; signature is not constructed")
        signature_cls = reflection_dspy.GenerateReflection
        # DSPy stores fields on the model_fields attribute (Pydantic BaseModel)
        fields = getattr(signature_cls, "model_fields", {}) or getattr(signature_cls, "fields", {})
        assert "missing_skill_names" in fields, (
            f"missing_skill_names not declared on GenerateReflection. Available: {list(fields)}"
        )
