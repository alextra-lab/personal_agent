"""Unit tests for ``default_extraction_summary`` (ADR-0125 D5, FRE-1002).

The extraction fallback and its consumer (``second_brain/consolidator.py``'s
``is_fallback`` detection) share this one function precisely so they cannot
drift — the retired ``user_message[:200] + "..."`` shape and the comparator's
own ``user_message[:200]`` (no suffix) could never compare equal for a message
over the cap, which these tests would have caught.
"""

from __future__ import annotations

from personal_agent.second_brain.entity_extraction import (
    DEFAULT_SUMMARY_CHAR_LIMIT,
    _default_extraction_result,
    default_extraction_summary,
)


def test_short_message_is_returned_unchanged() -> None:
    msg = "What is the meaning of life?"
    assert default_extraction_summary(msg) == msg


def test_long_message_is_marked_not_silently_clipped() -> None:
    msg = "x" * (DEFAULT_SUMMARY_CHAR_LIMIT + 100)
    result = default_extraction_summary(msg)
    assert len(result) < len(msg)
    assert f"...[truncated 100 chars]" in result  # noqa: F541


def test_default_extraction_result_uses_the_shared_summary() -> None:
    msg = "x" * (DEFAULT_SUMMARY_CHAR_LIMIT + 50)
    result = _default_extraction_result(msg)
    assert result["summary"] == default_extraction_summary(msg)
    assert result["entities"] == []
