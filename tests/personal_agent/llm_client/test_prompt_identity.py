"""Tests for the prompt identity primitive (ADR-0078 D1/D4, FRE-405, FRE-1008)."""

from __future__ import annotations

import dataclasses

import pytest

from personal_agent.llm_client.prompt_identity import (
    PromptIdentity,
    _serialize_dynamic_content,
    _short_hash,
    derive_fallback_prompt_identity,
    derive_orchestrator_prompt_identity,
    derive_prompt_identity,
)


class TestShortHash:
    def test_returns_16_hex_chars(self) -> None:
        h = _short_hash("some prompt text")
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic(self) -> None:
        assert _short_hash("abc") == _short_hash("abc")

    def test_distinct_inputs_distinct_hashes(self) -> None:
        assert _short_hash("abc") != _short_hash("abd")

    def test_empty_string_is_hashable(self) -> None:
        assert len(_short_hash("")) == 16


class TestPromptIdentity:
    def test_is_frozen(self) -> None:
        ident = derive_prompt_identity(
            "orchestrator.primary", static_prefix="s", full_prompt="s\nmem"
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            ident.callsite = "other"  # type: ignore[misc]

    def test_fields_populated(self) -> None:
        ident = derive_prompt_identity(
            "gateway.chat",
            static_prefix="persona",
            full_prompt="persona",
            component_ids=("gateway_persona",),
        )
        assert ident.callsite == "gateway.chat"
        assert ident.component_ids == ("gateway_persona",)
        assert ident.static_prefix_hash == _short_hash("persona")
        assert ident.dynamic_hash == _short_hash("persona")


class TestDeriveIdentity:
    def test_static_hash_changes_when_prefix_changes(self) -> None:
        a = derive_prompt_identity("c", static_prefix="prefix-A", full_prompt="prefix-A\nx")
        b = derive_prompt_identity("c", static_prefix="prefix-B", full_prompt="prefix-B\nx")
        assert a.static_prefix_hash != b.static_prefix_hash

    def test_static_hash_stable_when_only_dynamic_changes(self) -> None:
        """AC core: same static prefix, different memory tail → static hash stable,
        dynamic hash differs.
        """
        static_prefix = "tool_awareness\n\noperator+skill blocks"
        a = derive_prompt_identity(
            "orchestrator.primary",
            static_prefix=static_prefix,
            full_prompt=f"{static_prefix}\n## Memory\nrecall set ONE",
        )
        b = derive_prompt_identity(
            "orchestrator.primary",
            static_prefix=static_prefix,
            full_prompt=f"{static_prefix}\n## Memory\nrecall set TWO",
        )
        assert a.static_prefix_hash == b.static_prefix_hash
        assert a.dynamic_hash != b.dynamic_hash

    def test_default_component_ids_empty_tuple(self) -> None:
        ident = derive_prompt_identity("c", static_prefix="s", full_prompt="s")
        assert ident.component_ids == ()
        assert isinstance(ident, PromptIdentity)


class TestSerializeDynamicContent:
    """FRE-1008: structure-preserving serialization, not a naive text join."""

    def test_distinguishes_structure_not_just_text(self) -> None:
        one_message = [{"role": "user", "content": "ab"}]
        two_messages = [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
        ]
        assert _serialize_dynamic_content(one_message) != _serialize_dynamic_content(two_messages)

    def test_distinguishes_roles(self) -> None:
        as_user = [{"role": "user", "content": "hello"}]
        as_assistant = [{"role": "assistant", "content": "hello"}]
        assert _serialize_dynamic_content(as_user) != _serialize_dynamic_content(as_assistant)

    def test_distinguishes_non_text_block_count(self) -> None:
        """Codex/code-reviewer finding: a set-of-types collapse would make two
        images and one image serialize identically. Must not.
        """
        one_image = [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "https://a"}}],
            }
        ]
        two_images = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://a"}},
                    {"type": "image_url", "image_url": {"url": "https://b"}},
                ],
            }
        ]
        assert _serialize_dynamic_content(one_image) != _serialize_dynamic_content(two_images)

    def test_distinguishes_non_text_block_content(self) -> None:
        """Same block type, same count, different payload → must still differ."""
        image_a = [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "https://a"}}],
            }
        ]
        image_b = [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "https://b"}}],
            }
        ]
        assert _serialize_dynamic_content(image_a) != _serialize_dynamic_content(image_b)

    def test_distinguishes_tool_calls(self) -> None:
        same_content = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "1", "function": {"name": "search", "arguments": "{}"}}],
            }
        ]
        different_tool_call = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "1", "function": {"name": "browse", "arguments": "{}"}}],
            }
        ]
        assert _serialize_dynamic_content(same_content) != _serialize_dynamic_content(
            different_tool_call
        )

    def test_covers_tools(self) -> None:
        messages = [{"role": "user", "content": "hi"}]
        tools_a = [{"name": "digest", "parameters": {"a": 1}}]
        tools_b = [{"name": "digest", "parameters": {"a": 2}}]
        assert _serialize_dynamic_content(messages, tools_a) != _serialize_dynamic_content(
            messages, tools_b
        )

    def test_stable_across_none_and_empty_tools(self) -> None:
        messages = [{"role": "user", "content": "hi"}]
        assert _serialize_dynamic_content(messages, None) == _serialize_dynamic_content(
            messages, []
        )

    def test_deterministic(self) -> None:
        messages = [{"role": "user", "content": "hi"}]
        assert _serialize_dynamic_content(messages) == _serialize_dynamic_content(messages)


class TestDeriveOrchestratorPromptIdentity:
    """FRE-1008: dynamic_hash must cover request_messages/tools, not collapse to static_prefix."""

    def test_dynamic_hash_covers_request_messages(self) -> None:
        static_prefix = "tool_awareness\n\noperator+skill blocks"
        a = derive_orchestrator_prompt_identity(
            static_prefix=static_prefix,
            request_messages=[{"role": "user", "content": "recall set ONE"}],
        )
        b = derive_orchestrator_prompt_identity(
            static_prefix=static_prefix,
            request_messages=[{"role": "user", "content": "recall set TWO"}],
        )
        assert a.static_prefix_hash == b.static_prefix_hash
        assert a.dynamic_hash != b.dynamic_hash

    def test_dynamic_hash_covers_tools(self) -> None:
        static_prefix = "static"
        messages = [{"role": "user", "content": "hi"}]
        a = derive_orchestrator_prompt_identity(
            static_prefix=static_prefix,
            request_messages=messages,
            tools=[{"name": "search"}],
        )
        b = derive_orchestrator_prompt_identity(
            static_prefix=static_prefix,
            request_messages=messages,
            tools=[{"name": "browse"}],
        )
        assert a.static_prefix_hash == b.static_prefix_hash
        assert a.dynamic_hash != b.dynamic_hash

    def test_callsite_and_component_ids(self) -> None:
        ident = derive_orchestrator_prompt_identity(
            static_prefix="s",
            request_messages=[{"role": "user", "content": "hi"}],
            component_ids=("tool_awareness", "memory_section"),
        )
        assert ident.callsite == "orchestrator.primary"
        assert ident.component_ids == ("tool_awareness", "memory_section")

    def test_regression_old_inline_call_collapses_to_same_hash(self) -> None:
        """Documents the FRE-1008 defect: hashing static_prefix as full_prompt
        (the old executor.py behavior) always collapses both hashes — this is
        what derive_orchestrator_prompt_identity replaces.
        """
        static_prefix = "same string"
        old_broken = derive_prompt_identity(
            "orchestrator.primary", static_prefix=static_prefix, full_prompt=static_prefix
        )
        assert old_broken.static_prefix_hash == old_broken.dynamic_hash


class TestDeriveFallbackPromptIdentity:
    """FRE-1008: fixes the empty-string collapse and gives leaf callsites a real split."""

    def test_no_system_prompt_uses_embedded_system_message(self) -> None:
        ident = derive_fallback_prompt_identity(
            "role.entity_extraction",
            system_prompt=None,
            request_messages=[
                {"role": "system", "content": "Extract entities."},
                {"role": "user", "content": "Some transcript text."},
            ],
        )
        assert ident.static_prefix_hash == _short_hash("Extract entities.")
        assert ident.static_prefix_hash != _short_hash("")
        assert ident.dynamic_hash != _short_hash("")

    def test_dynamic_hash_covers_message_tail(self) -> None:
        system_prompt = "You are a helpful assistant."
        a = derive_fallback_prompt_identity(
            "role.sub_agent",
            system_prompt=system_prompt,
            request_messages=[{"role": "user", "content": "query A"}],
        )
        b = derive_fallback_prompt_identity(
            "role.sub_agent",
            system_prompt=system_prompt,
            request_messages=[{"role": "user", "content": "query B"}],
        )
        assert a.static_prefix_hash == b.static_prefix_hash
        assert a.dynamic_hash != b.dynamic_hash

    def test_truly_empty_is_still_hashable(self) -> None:
        ident = derive_fallback_prompt_identity(
            "role.primary", system_prompt=None, request_messages=[]
        )
        assert ident.static_prefix_hash == _short_hash("")
        assert len(ident.dynamic_hash) == 16

    def test_system_prompt_param_takes_precedence_over_embedded_message(self) -> None:
        ident = derive_fallback_prompt_identity(
            "role.primary",
            system_prompt="explicit system prompt",
            request_messages=[{"role": "system", "content": "should be ignored"}],
        )
        assert ident.static_prefix_hash == _short_hash("explicit system prompt")
