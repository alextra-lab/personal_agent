"""Tests for reflection-time capability-gap capture (FRE-328 follow-up).

`parse_missing_skill_names` is the pure parser called inside the DSPy worker
thread (no logging side-effects, just returns a clean list).

`emit_missing_skill_warnings` is the main-loop emitter called by
`reflection.generate_reflection_entry` after the to_thread returns; its
warnings reach Elasticsearch via the standard handler chain and feed
`InsightsEngine.detect_missing_skill_patterns`.
"""

import asyncio
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.captains_log import reflection_dspy
from personal_agent.captains_log.models import CaptainLogEntry, CaptainLogEntryType


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


class TestMissingSkillNamesPersistedOnEntry:
    """FRE-1340: the signal must survive on the persisted, reachable entry.

    Before this fix, ``missing_skill_names`` was consumed only by
    ``emit_missing_skill_warnings`` (a fire-and-forget log line) and then
    discarded — the ``CaptainLogEntry`` that ``CaptainLogManager.write_entry``
    actually persists to disk/ES never carried it.
    """

    @staticmethod
    def _entry() -> CaptainLogEntry:
        return CaptainLogEntry(
            entry_id="",
            type=CaptainLogEntryType.REFLECTION,
            title="t",
            rationale="r",
        )

    @contextmanager
    def _env(self, names: list[str]):
        def _fake_dspy(*args: Any, **kwargs: Any) -> tuple[CaptainLogEntry, list[str]]:
            return self._entry(), names

        async def _fake_to_thread(fn: Any, **kwargs: Any) -> Any:
            return fn(**kwargs)

        from personal_agent.config import load_model_config

        model_def = load_model_config().models["claude_sonnet"]

        with (
            patch(
                "personal_agent.captains_log.reflection._fetch_trace_events",
                AsyncMock(return_value=[]),
            ),
            patch("personal_agent.captains_log.reflection.DSPY_AVAILABLE", True),
            patch(
                "personal_agent.captains_log.reflection.generate_reflection_dspy", new=_fake_dspy
            ),
            patch.object(asyncio, "to_thread", new=_fake_to_thread),
            patch(
                "personal_agent.captains_log.reflection.load_mean_rating_lookup",
                new=AsyncMock(return_value={}),
            ),
            # Local (non-cloud) target — the missing-skill plumbing under test
            # is orthogonal to the FRE-989 cost-gate path already covered by
            # test_reflection_dspy_gated.py.
            patch(
                "personal_agent.captains_log.reflection.resolve_dspy_target",
                return_value=("claude_sonnet", model_def, False),
            ),
        ):
            yield

    @pytest.mark.asyncio
    async def test_missing_skill_names_attached_to_returned_entry(self) -> None:
        """The specific signal that motivated FRE-1340 must not vanish."""
        from personal_agent.captains_log.reflection import generate_reflection_entry

        with self._env(["citation-validator", "compliance-checker"]):
            entry = await generate_reflection_entry(
                user_message="hi",
                trace_id="trace-test",
                steps_count=1,
                final_state="COMPLETED",
                reply_length=5,
                session_id="sess-1",
            )

        assert entry.missing_skill_names == ["citation-validator", "compliance-checker"]

    @pytest.mark.asyncio
    async def test_no_missing_skills_defaults_to_empty_list(self) -> None:
        """No signal detected → the field stays an empty list, not absent/None."""
        from personal_agent.captains_log.reflection import generate_reflection_entry

        with self._env([]):
            entry = await generate_reflection_entry(
                user_message="hi",
                trace_id="trace-test",
                steps_count=1,
                final_state="COMPLETED",
                reply_length=5,
                session_id="sess-1",
            )

        assert entry.missing_skill_names == []
