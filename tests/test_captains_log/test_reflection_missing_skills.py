"""Tests for reflection-time capability-gap capture (FRE-328 follow-up).

`parse_missing_skill_names` is the pure parser called inside the DSPy worker
thread (no logging side-effects, just returns a clean list).

`emit_missing_skill_warnings` is the main-loop emitter called by
`reflection.generate_reflection_entry` after the to_thread returns; its
warnings reach Elasticsearch via the standard handler chain and feed
`InsightsEngine.detect_missing_skill_patterns`.
"""

from typing import Any

import pytest

from personal_agent.captains_log import reflection_dspy


class TestParseMissingSkillNames:
    """Behavioral contract for the pure parser."""

    def test_empty_input_returns_empty(self) -> None:
        """Empty string → empty list."""
        assert reflection_dspy.parse_missing_skill_names("", trace_id="t") == []

    def test_whitespace_only_returns_empty(self) -> None:
        """Whitespace and bare commas → empty list."""
        assert reflection_dspy.parse_missing_skill_names("  , , ", trace_id="t") == []

    def test_single_valid_name(self) -> None:
        """One valid kebab-case name passes through."""
        result = reflection_dspy.parse_missing_skill_names("slack-notify", trace_id="t")
        assert result == ["slack-notify"]

    def test_multiple_names_preserve_order(self) -> None:
        """Comma-separated names retain LLM-emitted order."""
        result = reflection_dspy.parse_missing_skill_names(
            "slack-notify, pagerduty-alert, github-release",
            trace_id="t",
        )
        assert result == ["slack-notify", "pagerduty-alert", "github-release"]

    def test_dedup_case_insensitive(self) -> None:
        """Case-variant duplicates collapse into the lowercased form."""
        result = reflection_dspy.parse_missing_skill_names(
            "slack-notify, Slack-Notify, SLACK-NOTIFY",
            trace_id="t",
        )
        assert result == ["slack-notify"]

    def test_invalid_names_rejected(self) -> None:
        """Names with spaces, underscores, or punctuation are silently dropped."""
        result = reflection_dspy.parse_missing_skill_names(
            "ok-name, bad name, bad_name!",
            trace_id="t",
        )
        assert result == ["ok-name"]

    def test_cap_at_max(self) -> None:
        """Output is capped at _MISSING_SKILL_MAX names."""
        result = reflection_dspy.parse_missing_skill_names(
            "skill-a, skill-b, skill-c, skill-d, skill-e",
            trace_id="t",
        )
        assert result == ["skill-a", "skill-b", "skill-c"]
        assert len(result) == reflection_dspy._MISSING_SKILL_MAX

    def test_lowercase_normalization(self) -> None:
        """Mixed-case input is lowercased so fingerprint dedup stays stable."""
        result = reflection_dspy.parse_missing_skill_names("Slack-Notify", trace_id="t")
        assert result == ["slack-notify"]


class TestEmitMissingSkillWarnings:
    """The main-loop emitter calls log.warning once per name."""

    def test_no_names_no_warnings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty list → no warning calls."""
        captured: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            reflection_dspy.log,
            "warning",
            lambda event, **kw: captured.append((event, kw)),
        )
        reflection_dspy.emit_missing_skill_warnings([], trace_id="t")
        assert captured == []

    def test_one_warning_per_name_with_correct_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each name produces one ``missing_skill_requested`` warning with the expected fields."""
        captured: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            reflection_dspy.log,
            "warning",
            lambda event, **kw: captured.append((event, kw)),
        )
        reflection_dspy.emit_missing_skill_warnings(
            ["slack-notify", "pagerduty-alert"],
            trace_id="trace-xyz",
            session_id="sess-abc",
        )
        assert len(captured) == 2
        events = [e for e, _ in captured]
        assert events == ["missing_skill_requested", "missing_skill_requested"]
        names = [kw["requested_name"] for _, kw in captured]
        assert names == ["slack-notify", "pagerduty-alert"]
        for _, kw in captured:
            assert kw["source"] == "reflection"
            assert kw["trace_id"] == "trace-xyz"
            assert kw["session_id"] == "sess-abc"

    def test_session_id_defaults_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When session_id isn't passed, the warning still emits with session_id=None."""
        captured: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            reflection_dspy.log,
            "warning",
            lambda event, **kw: captured.append((event, kw)),
        )
        reflection_dspy.emit_missing_skill_warnings(["s"], trace_id="t")
        assert len(captured) == 1
        assert captured[0][1]["session_id"] is None


class TestDspySignatureField:
    """The `missing_skill_names` field must be part of the DSPy reflection contract."""

    def test_signature_declares_field_when_dspy_available(self) -> None:
        """When dspy is installed, the GenerateReflection class exposes the new output field."""
        if not reflection_dspy.DSPY_AVAILABLE:
            pytest.skip("dspy not installed; signature is not constructed")
        signature_cls = reflection_dspy.GenerateReflection
        fields = getattr(signature_cls, "model_fields", {}) or getattr(signature_cls, "fields", {})
        assert "missing_skill_names" in fields, (
            f"missing_skill_names not declared on GenerateReflection. Available: {list(fields)}"
        )
