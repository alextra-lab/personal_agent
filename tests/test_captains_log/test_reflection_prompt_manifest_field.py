"""Tests verifying prompt_manifest is wired into the reflection pipeline (FRE-409).

Pre-merge ACs:
  - GenerateReflection DSPy signature gains a prompt_manifest input field.
  - REFLECTION_PROMPT manual template contains {prompt_manifest}.
  - generate_reflection_entry builds and passes a non-empty manifest when
    the trace has an identity-bearing model_call_completed event.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestGenerateReflectionSignatureField:
    """prompt_manifest must be declared on GenerateReflection."""

    def test_signature_declares_prompt_manifest_input_field_when_dspy_available(
        self,
    ) -> None:
        from personal_agent.captains_log import reflection_dspy

        if not reflection_dspy.DSPY_AVAILABLE:
            pytest.skip("dspy not installed; signature is not constructed")

        signature_cls = reflection_dspy.GenerateReflection
        fields = getattr(signature_cls, "model_fields", {}) or getattr(
            signature_cls, "fields", {}
        )
        assert "prompt_manifest" in fields, (
            f"prompt_manifest not declared on GenerateReflection. "
            f"Available fields: {list(fields)}"
        )


class TestReflectionPromptTemplate:
    """The manual REFLECTION_PROMPT template must contain the placeholder."""

    def test_reflection_prompt_contains_prompt_manifest_placeholder(self) -> None:
        from personal_agent.captains_log.reflection import REFLECTION_PROMPT

        assert "{prompt_manifest}" in REFLECTION_PROMPT, (
            "REFLECTION_PROMPT must contain {prompt_manifest} so the manual "
            "fallback path threads the manifest into the LLM context."
        )


class TestGenerateReflectionDspyPassthrough:
    """generate_reflection_dspy accepts and passes prompt_manifest to ChainOfThought."""

    def test_accepts_prompt_manifest_param(self) -> None:
        """The function signature includes prompt_manifest with a default of ''."""
        import inspect

        from personal_agent.captains_log.reflection_dspy import generate_reflection_dspy

        sig = inspect.signature(generate_reflection_dspy)
        assert "prompt_manifest" in sig.parameters, (
            "generate_reflection_dspy must declare prompt_manifest parameter"
        )
        param = sig.parameters["prompt_manifest"]
        assert param.default == "", (
            "prompt_manifest default must be '' so existing callers work unchanged"
        )


class TestGenerateReflectionEntryBuildsManifest:
    """generate_reflection_entry builds a manifest from trace events and threads it."""

    @pytest.mark.asyncio
    async def test_non_empty_manifest_when_trace_has_identity_event(self) -> None:
        """When get_trace_events returns an identity event, manifest is non-empty."""
        import asyncio

        identity_event: dict[str, Any] = {
            "event_type": "model_call_completed",
            "prompt_callsite": "orchestrator.primary",
            "prompt_component_ids": ["tool_awareness", "skill_index"],
            "prompt_static_prefix_hash": "aabbccddeeff0011",
            "prompt_dynamic_hash": "0011223344556677",
        }

        captured_manifest: list[str] = []

        def fake_dspy(*args: Any, prompt_manifest: str = "", **kwargs: Any) -> Any:
            captured_manifest.append(prompt_manifest)
            from personal_agent.captains_log.models import CaptainLogEntry, CaptainLogEntryType

            return (
                CaptainLogEntry(
                    entry_type=CaptainLogEntryType.REFLECTION,
                    title="t",
                    rationale="r",
                    proposed_change=None,
                    supporting_metrics=[],
                    impact_assessment=None,
                    related_adrs=[],
                    related_experiments=[],
                    trace_id="trace-test",
                ),
                [],
            )

        async def fake_to_thread(fn: Any, **kwargs: Any) -> Any:
            return fn(**kwargs)

        with (
            patch(
                "personal_agent.captains_log.reflection.get_trace_events",
                return_value=[identity_event],
            ),
            patch(
                "personal_agent.captains_log.reflection.DSPY_AVAILABLE",
                True,
            ),
            patch(
                "personal_agent.captains_log.reflection.generate_reflection_dspy",
                new=fake_dspy,
            ),
            patch.object(asyncio, "to_thread", new=fake_to_thread),
            patch(
                "personal_agent.captains_log.reflection.load_mean_rating_lookup",
                new=AsyncMock(return_value={}),
            ),
        ):
            from personal_agent.captains_log.reflection import generate_reflection_entry

            await generate_reflection_entry(
                user_message="hi",
                trace_id="trace-test",
                steps_count=1,
                final_state="COMPLETED",
                reply_length=5,
            )

        assert captured_manifest, "generate_reflection_dspy was not called"
        manifest = captured_manifest[0]
        assert manifest != "Prompt manifest: unavailable", (
            "Expected a non-unavailable manifest when identity event is present"
        )
        assert "tool_awareness" in manifest
        assert "aabbccddeeff0011" in manifest
