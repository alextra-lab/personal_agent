"""Tests for ``mark_truncated`` (ADR-0125 D5 — no silent truncation on an evidence path).

D5 requires content on an evidence path to be stored whole, or shortened with an
explicit marker recording that it was shortened and by how much. These tests assert
the marker's actual shape and edge behavior, since the guard
(``scripts/check_evidence_truncation.py``) trusts this function by import binding to
be the one compliant shortening path.
"""

from __future__ import annotations

from personal_agent.captains_log.turn_evidence import mark_truncated


def test_text_under_limit_is_unchanged() -> None:
    assert mark_truncated("short", 200) == "short"


def test_text_at_exact_limit_is_unchanged_no_marker() -> None:
    text = "x" * 200
    assert mark_truncated(text, 200) == text


def test_text_over_limit_gets_head_plus_marker() -> None:
    text = "a" * 250
    result = mark_truncated(text, 200)
    assert result.startswith("a" * 200)
    assert result == "a" * 200 + "...[truncated 50 chars]"


def test_bytes_unit_counts_utf8_encoded_length() -> None:
    # Each "é" is 2 bytes in UTF-8, so 10 chars = 20 bytes.
    text = "é" * 10
    result = mark_truncated(text, 10, unit="bytes")
    assert "...[truncated 10 bytes]" in result
    # The kept head must itself be valid UTF-8 (no split multibyte tail).
    assert result.split("...[truncated")[0].encode("utf-8")


def test_bytes_unit_under_limit_is_unchanged() -> None:
    text = "é" * 3  # 6 bytes
    assert mark_truncated(text, 10, unit="bytes") == text


def test_multibyte_boundary_does_not_produce_invalid_output() -> None:
    # 5 chars of "é" (2 bytes each) = 10 bytes; a 9-byte limit lands mid-character.
    text = "é" * 5
    result = mark_truncated(text, 9, unit="bytes")
    # Must not raise, and the kept head must decode cleanly (errors="ignore" applied).
    head = result.split("...[truncated")[0]
    assert head.encode("utf-8").decode("utf-8") == head


def test_marker_names_exact_chars_omitted() -> None:
    text = "z" * 1847  # p50 assistant-response length cited in ADR-0125 D5
    result = mark_truncated(text, 400)
    assert "...[truncated 1447 chars]" in result
