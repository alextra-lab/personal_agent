# tests/personal_agent/request_gateway/test_recall_controller.py
"""Tests for the recall controller (Stage 4b).

Three-gate design:
1. Task type gate: only CONVERSATIONAL enters
2. Cue pattern gate: implicit backward-reference cues
3. Session fact gate: noun phrase corroboration in session history
"""

from __future__ import annotations

import pytest

from personal_agent.request_gateway.recall_controller import (
    _RECALL_CUE_PATTERNS,
    _detect_recall_cues,
    _extract_noun_phrases,
    _scan_session_facts,
    run_recall_controller,
)
from personal_agent.request_gateway.types import (
    Complexity,
    IntentResult,
    TaskType,
)


class TestDetectRecallCues:
    def test_again_with_question(self) -> None:
        assert _detect_recall_cues("What was our primary database again?") is not None

    def test_going_back(self) -> None:
        assert _detect_recall_cues("Going back to the beginning — what was our database?") is not None

    def test_remind_me(self) -> None:
        assert _detect_recall_cues("Remind me what we decided on caching") is not None

    def test_what_did_we_decide(self) -> None:
        assert _detect_recall_cues("What did we decide on the API framework?") is not None

    def test_the_thing_we_discussed(self) -> None:
        assert _detect_recall_cues("The framework we discussed earlier") is not None

    def test_no_cue_simple_question(self) -> None:
        assert _detect_recall_cues("What is the weather today?") is None

    def test_no_cue_bare_again(self) -> None:
        """Bare 'again' without interrogative context should not trigger."""
        assert _detect_recall_cues("Let's try that approach again") is None

    def test_no_cue_conversational(self) -> None:
        assert _detect_recall_cues("Tell me something interesting") is None


class TestExtractNounPhrases:
    def test_extracts_primary_database(self) -> None:
        phrases = _extract_noun_phrases("What was our primary database again?")
        assert any("database" in p.lower() for p in phrases)

    def test_extracts_api_framework(self) -> None:
        phrases = _extract_noun_phrases("What did we decide on the API framework?")
        assert any("framework" in p.lower() for p in phrases)


class TestScanSessionFacts:
    def test_finds_matching_fact(self) -> None:
        session_messages = [
            {"role": "user", "content": "Let's use PostgreSQL as our primary database"},
            {"role": "assistant", "content": "Great choice! PostgreSQL is excellent for this."},
            {"role": "user", "content": "Now let's discuss caching..."},
            {"role": "assistant", "content": "Sure, let's look at Redis."},
        ]
        candidates = _scan_session_facts(
            noun_phrases=["primary database"],
            session_messages=session_messages,
            max_candidates=3,
        )
        assert len(candidates) >= 1
        assert any("PostgreSQL" in c.fact or "database" in c.fact for c in candidates)

    def test_no_match_returns_empty(self) -> None:
        session_messages = [
            {"role": "user", "content": "Hello, how are you?"},
            {"role": "assistant", "content": "I'm doing well!"},
        ]
        candidates = _scan_session_facts(
            noun_phrases=["primary database"],
            session_messages=session_messages,
            max_candidates=3,
        )
        assert len(candidates) == 0


class TestRunRecallController:
    def test_non_conversational_passes_through(self) -> None:
        """Non-CONVERSATIONAL intents skip the controller entirely."""
        intent = IntentResult(
            task_type=TaskType.ANALYSIS,
            complexity=Complexity.MODERATE,
            confidence=0.9,
            signals=["reasoning_patterns"],
        )
        result = run_recall_controller(
            intent=intent,
            user_message="Analyze Redis performance",
            session_messages=[],
        )
        assert result is None  # No reclassification

    def test_conversational_no_cue_passes_through(self) -> None:
        """CONVERSATIONAL without recall cues passes through."""
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.7,
            signals=[],
        )
        result = run_recall_controller(
            intent=intent,
            user_message="Tell me something interesting",
            session_messages=[],
        )
        assert result is None

    def test_reclassifies_implicit_recall(self) -> None:
        """Implicit recall with corroborating session fact → reclassify."""
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.7,
            signals=[],
        )
        session = [
            {"role": "user", "content": "Let's use PostgreSQL as our primary database"},
            {"role": "assistant", "content": "PostgreSQL is a great choice."},
            {"role": "user", "content": "Now let's talk about the API layer."},
            {"role": "assistant", "content": "Sure, FastAPI is our framework."},
        ]
        result = run_recall_controller(
            intent=intent,
            user_message="Going back to the beginning — what was our primary database again?",
            session_messages=session,
        )
        assert result is not None
        assert result.reclassified is True
        assert result.original_task_type == TaskType.CONVERSATIONAL
        assert len(result.candidates) >= 1

    def test_cue_without_session_match_no_reclassify(self) -> None:
        """Cue detected but no corroborating fact → false positive, no reclassify."""
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.7,
            signals=[],
        )
        result = run_recall_controller(
            intent=intent,
            user_message="What was our primary database again?",
            session_messages=[
                {"role": "user", "content": "Hello!"},
                {"role": "assistant", "content": "Hi there!"},
            ],
        )
        # Cue fires but no session fact corroboration → no reclassify
        assert result is None or result.reclassified is False


@pytest.mark.parametrize(
    ("text", "should_match"),
    [
        ("What was that data processing tool we discussed?", True),
        ("What database did we decide on?", True),
        (
            "Going back to the beginning — what was our primary database again?",
            True,
        ),
        ("What was our primary database again?", True),
        ("Going back to earlier — what caching system did we pick?", True),
        ("Remind me what we decided on the message queue?", True),
        ("What did we decide on the CI/CD pipeline?", True),
        (
            "Refresh my memory — what was our main programming language?",
            True,
        ),
        (
            "The tool we discussed earlier — can you confirm what it was?",
            True,
        ),
        ("What is dependency injection?", False),
        ("Tell me about Redis performance.", False),
    ],
)
def test_recall_cue_patterns_cp19_variants(text: str, should_match: bool) -> None:
    """Verify _RECALL_CUE_PATTERNS matches all CP-19 variant inputs."""
    match = _RECALL_CUE_PATTERNS.search(text)
    if should_match:
        assert match is not None, f"Pattern should match: {text!r}"
    else:
        assert match is None, f"Pattern should NOT match: {text!r}"


def test_recall_cue_telemetry_when_intent_already_memory_recall(caplog: pytest.LogCaptureFixture) -> None:
    """recall_cue_detected is emitted even when Stage 4 already classified MEMORY_RECALL."""
    caplog.set_level("INFO", logger="personal_agent.request_gateway.recall_controller")

    intent = IntentResult(
        task_type=TaskType.MEMORY_RECALL,
        complexity=Complexity.SIMPLE,
        confidence=0.9,
        signals=["memory_recall_pattern"],
    )
    result = run_recall_controller(
        intent=intent,
        user_message="What did we decide on the CI/CD pipeline?",
        session_messages=[],
        trace_id="t-telemetry",
    )
    assert result is None
    assert "recall_cue_detected" in caplog.text
