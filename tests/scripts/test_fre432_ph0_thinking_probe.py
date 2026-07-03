"""Unit tests for the FRE-432 Phase-0 thinking-token probe pure helpers.

Only the pure computation layer is tested here (no DB / ES / SLM I/O): the
think/visible extraction, the char-share and token-estimate math, and the
distribution summariser. The live-substrate and replay paths are exercised by
running the probe against the deployed stack, not by unit tests.
"""

from __future__ import annotations

import pytest
from scripts.research.fre432_ph0_thinking_probe import (
    estimate_think_tokens,
    extract_think_visible,
    percentile,
    summarize_tokens,
    think_share,
)


class TestExtractThinkVisible:
    """`extract_think_visible` recovers (think, visible) across backend shapes."""

    def test_reasoning_content_field(self) -> None:
        """llama.cpp splits thinking into a dedicated ``reasoning_content`` field."""
        msg = {"content": "The answer is 42.", "reasoning_content": "Let me think hard."}
        think, visible = extract_think_visible(msg)
        assert think == "Let me think hard."
        assert visible == "The answer is 42."

    def test_inline_closed_think_tag(self) -> None:
        """Some backends emit ``<think>...</think>`` inline in ``content``."""
        msg = {"content": "<think>reasoning here</think>Final answer.", "reasoning_content": ""}
        think, visible = extract_think_visible(msg)
        assert think == "reasoning here"
        assert visible == "Final answer."

    def test_inline_unclosed_think_tag(self) -> None:
        """A truncated generation may leave ``<think>`` open with no closer."""
        msg = {"content": "<think>still thinking and cut off", "reasoning_content": None}
        think, visible = extract_think_visible(msg)
        assert think == "still thinking and cut off"
        assert visible == ""

    def test_no_thinking(self) -> None:
        """A plain answer with no thinking yields an empty think string."""
        msg = {"content": "Just an answer.", "reasoning_content": None}
        think, visible = extract_think_visible(msg)
        assert think == ""
        assert visible == "Just an answer."

    def test_reasoning_field_takes_precedence_over_inline(self) -> None:
        """When both are present the dedicated field wins (llama.cpp canonical shape)."""
        msg = {"content": "answer", "reasoning_content": "field-thoughts"}
        think, visible = extract_think_visible(msg)
        assert think == "field-thoughts"
        assert visible == "answer"


class TestThinkShare:
    """`think_share` is the character share of thinking in the total generation."""

    def test_half_and_half(self) -> None:
        """Equal think/visible lengths give a 0.5 share."""
        assert think_share("abcd", "abcd") == pytest.approx(0.5)

    def test_all_thinking(self) -> None:
        """No visible text gives a full 1.0 share."""
        assert think_share("abcd", "") == pytest.approx(1.0)

    def test_no_generation_is_zero(self) -> None:
        """Empty generation must not divide by zero."""
        assert think_share("", "") == 0.0


class TestEstimateThinkTokens:
    """`estimate_think_tokens` apportions completion tokens by the think share."""

    def test_basic(self) -> None:
        """A 70% share of 1000 tokens is 700 thinking tokens."""
        assert estimate_think_tokens(1000, 0.7) == 700

    def test_rounds_to_nearest_int(self) -> None:
        """The token estimate rounds to a whole number."""
        assert estimate_think_tokens(101, 0.5) == 50  # 50.5 -> banker's rounding to 50

    def test_none_completion_tokens(self) -> None:
        """A missing provider token count yields ``None`` (unknowable, not zero)."""
        assert estimate_think_tokens(None, 0.7) is None


class TestPercentile:
    """`percentile` on a pre-sorted list, clamped at the top index."""

    def test_median(self) -> None:
        """The 0.5 percentile indexes the mid element."""
        assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 3.0

    def test_p100_clamps_to_last(self) -> None:
        """A 1.0 percentile clamps to the final element, not out of range."""
        assert percentile([1.0, 2.0, 3.0], 1.0) == 3.0

    def test_empty(self) -> None:
        """An empty sequence returns 0.0."""
        assert percentile([], 0.5) == 0.0


class TestSummarizeTokens:
    """`summarize_tokens` reports the distribution fields the note quotes."""

    def test_summary_fields(self) -> None:
        """The summary reports n, median, max and mean."""
        summary = summarize_tokens([10, 20, 30, 40, 1000])
        assert summary["n"] == 5
        assert summary["median"] == 30
        assert summary["max"] == 1000
        assert summary["mean"] == pytest.approx(220.0)

    def test_empty_summary(self) -> None:
        """An empty distribution summarises to all-zero fields."""
        summary = summarize_tokens([])
        assert summary["n"] == 0
        assert summary["median"] == 0
        assert summary["max"] == 0
