"""Tests for the prompt identity primitive (ADR-0078 D1/D4, FRE-405)."""

from __future__ import annotations

import dataclasses

import pytest
from personal_agent.llm_client.prompt_identity import (
    PromptIdentity,
    _short_hash,
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
