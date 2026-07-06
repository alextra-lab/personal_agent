"""Tests for the reflection producers tagging ProposalSource.REFLECTION (ADR-0105 D1).

Covers both reflection code paths: the manual-fallback builder in
``reflection.py`` and the DSPy-based generator in ``reflection_dspy.py``.
"""

from unittest.mock import MagicMock, patch

import personal_agent.captains_log.reflection_dspy as reflection_dspy_module
from personal_agent.captains_log.models import ProposalSource
from personal_agent.captains_log.reflection import _build_proposed_change
from personal_agent.captains_log.reflection_dspy import generate_reflection_dspy


class TestBuildProposedChangeTagsReflectionSource:
    """_build_proposed_change (manual-fallback path) tags source=REFLECTION."""

    def test_tags_reflection_source(self) -> None:
        pc = _build_proposed_change(
            {
                "what": "Add retry logic",
                "why": "Improves reliability",
                "how": "Wrap calls in tenacity",
                "category": "reliability",
                "scope": "llm_client",
            }
        )
        assert pc is not None
        assert pc.source == ProposalSource.REFLECTION

    def test_none_raw_returns_none(self) -> None:
        assert _build_proposed_change(None) is None


class _FakeDspyResult:
    """Minimal stand-in for a DSPy ChainOfThought prediction result."""

    def __init__(self) -> None:
        self.rationale = "Because reasons"
        self.proposed_change_what = "Do the thing"
        self.proposed_change_why = "It helps"
        self.proposed_change_how = "Like this"
        self.proposed_change_category = "reliability"
        self.proposed_change_scope = "llm_client"


class TestGenerateReflectionDspyTagsReflectionSource:
    """generate_reflection_dspy tags source=REFLECTION on the DSPy path."""

    def test_tags_reflection_source(self) -> None:
        fake_predictor = MagicMock(return_value=_FakeDspyResult())
        fake_dspy = MagicMock()
        fake_dspy.ChainOfThought.return_value = fake_predictor

        llm_client = MagicMock()
        llm_client.get_dspy_lm.return_value = MagicMock()

        with patch.object(reflection_dspy_module, "dspy", fake_dspy):
            entry, _missing_skills = generate_reflection_dspy(
                user_message="hi",
                trace_id="trace-1",
                steps_count=1,
                final_state="COMPLETED",
                reply_length=5,
                telemetry_summary="none",
                llm_client=llm_client,
            )

        assert entry.proposed_change is not None
        assert entry.proposed_change.source == ProposalSource.REFLECTION
