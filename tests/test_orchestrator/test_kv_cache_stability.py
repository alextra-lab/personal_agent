"""Tests for KV cache prefix stability — Phase 4.6.

Verifies that the system prompt (first message) is preserved byte-identical
across multiple apply_context_window() calls within a session, enabling
provider-side KV cache reuse.
"""

from __future__ import annotations

from typing import Any

from personal_agent.orchestrator.context_window import (
    apply_context_window,
    compute_prefix_hash,
)


def _msg(role: str, content: str) -> dict[str, Any]:
    return {"role": role, "content": content}


class TestComputePrefixHash:
    def test_same_message_same_hash(self) -> None:
        msg = _msg("system", "You are a helpful assistant.")
        assert compute_prefix_hash(msg) == compute_prefix_hash(msg)

    def test_different_content_different_hash(self) -> None:
        a = _msg("system", "You are a helpful assistant.")
        b = _msg("system", "You are a coding assistant.")
        assert compute_prefix_hash(a) != compute_prefix_hash(b)

    def test_different_role_different_hash(self) -> None:
        a = _msg("system", "Hello")
        b = _msg("user", "Hello")
        assert compute_prefix_hash(a) != compute_prefix_hash(b)

    def test_hash_is_12_hex_chars(self) -> None:
        msg = _msg("system", "test")
        h = compute_prefix_hash(msg)
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)


class TestPrefixStabilityAcrossTurns:
    """Simulate multiple turns and verify the system prompt is preserved."""

    def test_system_prompt_preserved_across_truncation_cycles(self) -> None:
        system_prompt = _msg("system", "You are a helpful AI assistant.")

        messages_turn_1 = [system_prompt] + [
            _msg("user" if i % 2 == 0 else "assistant", f"Turn content {i}" * 20)
            for i in range(10)
        ]
        messages_turn_2 = messages_turn_1 + [
            _msg("user", "Another question" * 20),
            _msg("assistant", "Another response" * 20),
        ]

        out_1 = apply_context_window(
            messages_turn_1, max_tokens=300, reserved_tokens=0
        )
        out_2 = apply_context_window(
            messages_turn_2, max_tokens=300, reserved_tokens=0
        )

        assert out_1[0] == system_prompt
        assert out_2[0] == system_prompt
        assert compute_prefix_hash(out_1[0]) == compute_prefix_hash(out_2[0])

    def test_prefix_stable_with_compressed_summary(self) -> None:
        system_prompt = _msg("system", "You are a helpful AI assistant.")

        messages = [system_prompt] + [
            _msg("user" if i % 2 == 0 else "assistant", f"content {i}" * 20)
            for i in range(10)
        ]

        out_no_summary = apply_context_window(
            messages, max_tokens=300, reserved_tokens=0
        )
        out_with_summary = apply_context_window(
            messages,
            max_tokens=300,
            reserved_tokens=0,
            compressed_summary="Summary of earlier turns",
        )

        assert out_no_summary[0] == system_prompt
        assert out_with_summary[0] == system_prompt
        assert compute_prefix_hash(out_no_summary[0]) == compute_prefix_hash(
            out_with_summary[0]
        )

    def test_compression_summary_at_index_1_not_index_0(self) -> None:
        """The compressed summary should be at index 1, not replacing index 0."""
        system_prompt = _msg("system", "You are a helpful AI assistant.")
        summary = "Earlier conversation summary"

        messages = [system_prompt] + [
            _msg("user" if i % 2 == 0 else "assistant", f"content {i}" * 20)
            for i in range(10)
        ]

        output = apply_context_window(
            messages,
            max_tokens=300,
            reserved_tokens=0,
            compressed_summary=summary,
        )

        assert output[0] == system_prompt
        summary_msgs = [m for m in output if m.get("content") == summary]
        for s in summary_msgs:
            assert output.index(s) >= 1
