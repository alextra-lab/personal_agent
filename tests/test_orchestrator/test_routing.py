"""Tests for routing: heuristic classification + two-tier model taxonomy (ADR-0033)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.config import settings
from personal_agent.exceptions import AttachmentUnsupportedError
from personal_agent.governance.models import Mode
from personal_agent.llm_client import ModelRole
from personal_agent.llm_client.models import (
    ModelConfig,
    ModelDefinition,
    Placement,
    ProviderDefinition,
    RoleBinding,
)
from personal_agent.orchestrator import Channel, Orchestrator
from personal_agent.orchestrator.executor import (
    _determine_initial_model_role,
    _resolve_document_routing_key,
    _resolve_vision_routing_key,
)
from personal_agent.orchestrator.routing import (
    heuristic_routing,
    is_memory_recall_query,
    resolve_role,
)
from personal_agent.orchestrator.types import AttachmentRef, ExecutionContext
from tests.test_orchestrator.conftest import configure_mock_llm_client_model_configs


class TestRoutingHelpers:
    """Unit tests for routing helper functions."""

    def test_heuristic_gate_coding(self) -> None:
        """Routes stack-trace/code-like input to PRIMARY (two-tier taxonomy)."""
        plan = heuristic_routing("Debug this stack trace: Traceback ... def foo():")
        assert plan["target_model"] == ModelRole.PRIMARY
        assert plan["used_heuristics"] is True

    def test_heuristic_gate_standard_tool_intent(self) -> None:
        """Routes explicit web/tool intent to PRIMARY."""
        plan = heuristic_routing("Please search web for latest news on Rust")
        assert plan["target_model"] == ModelRole.PRIMARY

    def test_heuristic_gate_reasoning(self) -> None:
        """Routes formal proof style prompts to PRIMARY."""
        plan = heuristic_routing("Prove this rigorously with multi-step formal analysis")
        assert plan["target_model"] == ModelRole.PRIMARY

    def test_is_memory_recall_query_positive_cases(self) -> None:
        """ADR-0025: recall intent detected for history questions."""
        positive = [
            "What Greek locations have I asked about in the past?",
            "What have I ever asked you about?",
            "What topics have I discussed with you?",
            "What things have I mentioned?",
            "Have I ever asked about Paris?",
            "Have I mentioned my trip to Rome?",
            "Did I ask about the weather?",
            "Did I talk about Python?",
            "Do you remember what we discussed?",
            "My past conversation about travel",
            "Our previous session on cooking",
            "Last time we talked about books",
            "Remind me what we covered",
            "Remind me about that project",
            "What else have we talked about?",
            "What have we discussed so far?",
            # Eval CP-26 turn 4: broad recall (executor memory_recall_broad_query)
            "What do you remember about the DataForge project?",
        ]
        for msg in positive:
            assert is_memory_recall_query(msg), f"Expected recall: {msg!r}"

    def test_is_memory_recall_query_negative_cases(self) -> None:
        """ADR-0025: no recall intent for task-assist or other queries."""
        negative = [
            "What is the weather in Crete?",
            "What is the capital of France?",
            "Search the web for news",
            "Tell me about Python",
            "How do I install Rust?",
            "Debug this stack trace",
            "Write a function to add two numbers",
            "What time is it?",
            "Hello",
            "Thanks",
            "Prove this rigorously",
            "List files in the current directory",
            "Open url https://example.com",
            "What Greek locations are worth visiting?",  # not "have I asked"
            "Have you seen the report?",  # "have you" not "have I"
        ]
        for msg in negative:
            assert not is_memory_recall_query(msg), f"Expected non-recall: {msg!r}"

    def test_is_memory_recall_query_empty_or_none(self) -> None:
        """None or empty message is not recall."""
        assert not is_memory_recall_query("")
        assert not is_memory_recall_query(None)  # type: ignore[arg-type]

    def test_resolve_role_primary_maps_to_primary(self, monkeypatch: Any) -> None:
        """Identity mapping: PRIMARY → PRIMARY (two-tier taxonomy, ADR-0033)."""
        monkeypatch.setattr(settings, "enable_reasoning_role", False)
        assert resolve_role(ModelRole.PRIMARY) == ModelRole.PRIMARY

    def test_resolve_role_sub_agent_maps_to_sub_agent(self) -> None:
        """Identity mapping: SUB_AGENT → SUB_AGENT (ADR-0033)."""
        assert resolve_role(ModelRole.SUB_AGENT) == ModelRole.SUB_AGENT

    def test_determine_initial_model_role_chat(self, monkeypatch: Any) -> None:
        """Starts chat channel on PRIMARY role (two-tier taxonomy)."""
        monkeypatch.setattr(settings, "router_role", "PRIMARY")
        ctx = MagicMock(spec=ExecutionContext)
        ctx.channel = Channel.CHAT
        assert _determine_initial_model_role(ctx) == ModelRole.PRIMARY


@pytest.mark.asyncio
class TestRoutingFlow:
    """Integration tests for orchestrator routing with two-tier taxonomy (ADR-0033)."""

    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_chat_request_uses_primary_model(self, mock_client_class: Any) -> None:
        """All chat requests route directly to PRIMARY — no router LLM call (ADR-0033)."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client
        mock_client.respond.return_value = {
            "role": "assistant",
            "content": "Answer to Python question",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 80},
            "response_id": None,
            "raw": {},
        }

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="What is Python?",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        # Single LLM call — no router step
        assert mock_client.respond.call_count == 1
        call = mock_client.respond.call_args_list[0]
        assert call.kwargs["role"] == ModelRole.PRIMARY
        assert "What is Python?" in str(call.kwargs["messages"])

    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_single_model_mode_uses_primary_for_chat(
        self, mock_client_class: Any, monkeypatch: Any
    ) -> None:
        """PRIMARY is always used for chat requests in two-tier model (ADR-0033)."""
        monkeypatch.setattr(settings, "router_role", "PRIMARY")
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client
        mock_client.respond.return_value = {
            "role": "assistant",
            "content": "single model response",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 20},
            "response_id": None,
            "raw": {},
        }

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="hello",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        assert mock_client.respond.call_count == 1
        assert mock_client.respond.call_args.kwargs["role"] == ModelRole.PRIMARY



class TestVisionRouting:
    """ADR-0121 T5 (FRE-920): vision is a pinned Layer-3 role — no per-attachment
    override, no profile-driven escalation. ``_resolve_vision_routing_key``
    resolves the ``vision`` binding unconditionally whenever a raster image is
    present, and fails closed only if the pinned deployment itself lacks
    ``supports_vision`` (a config-drift guard, not a runtime routing choice).
    """

    def _make_attachment(self, **overrides: object) -> AttachmentRef:
        defaults: dict[str, object] = {
            "artifact_id": "abc-123",
            "content_type": "image/png",
            "title": "photo.png",
            "r2_key": "upload/user/GLOBAL/abc.png",
        }
        defaults.update(overrides)
        return AttachmentRef(**defaults)  # type: ignore[arg-type]

    def _make_ctx(self, attachments: tuple[AttachmentRef, ...]) -> ExecutionContext:
        return ExecutionContext(
            session_id="s1",
            trace_id="t1",
            user_message="hello",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            attachments=attachments,
        )

    def _model_def(self, *, supports_vision: bool, provider_type: str = "local") -> ModelDefinition:
        """Build a deployment whose PROVIDER carries the intended placement.

        `provider_type` is kept as the parameter name so the call sites read the
        same, but ADR-0121 deleted that field: placement is a provider fact now,
        so this maps "local"/"cloud" onto a provider declared in _patch_models.
        """
        return ModelDefinition(
            id="test-model",
            context_length=8192,
            max_concurrency=1,
            default_timeout=30,
            provider="slm_local" if provider_type == "local" else "anthropic",
            supports_vision=supports_vision,
        )

    def _patch_models(
        self, models: dict[str, ModelDefinition], *, vision_deployment: str | None = None
    ) -> Any:
        """Patch in a REAL ModelConfig, not a MagicMock.

        A MagicMock's `placement_of` returns a MagicMock, which is never
        `Placement.LOCAL` — so every deployment would silently read as cloud and
        the local-routing assertions would pass or fail for the wrong reason.
        Building the real object makes placement resolution genuine.

        `vision_deployment` seeds a Layer-3 `vision` role binding pointing at
        that model key — omit to test the "vision role not configured" case.
        """
        roles = {}
        if vision_deployment is not None:
            roles["vision"] = RoleBinding(deployment=vision_deployment)
        return patch(
            "personal_agent.config.model_loader.load_model_config",
            return_value=ModelConfig(
                providers={
                    "slm_local": ProviderDefinition(placement=Placement.LOCAL, max_concurrency=2),
                    "anthropic": ProviderDefinition(placement=Placement.CLOUD, max_concurrency=50),
                },
                models=models,
                roles=roles,
            ),
        )

    def test_no_image_attachment_is_noop(self) -> None:
        """No raster image attachment — returns the calling role's own resolved key."""
        ctx = self._make_ctx(())
        assert _resolve_vision_routing_key(ctx, "primary") == "qwen3.6-35b-thinking"

    def test_non_raster_attachment_is_noop(self) -> None:
        """A PDF attachment (ADR-0102 territory) never triggers vision routing."""
        ctx = self._make_ctx((self._make_attachment(content_type="application/pdf"),))
        assert _resolve_vision_routing_key(ctx, "primary") == "qwen3.6-35b-thinking"

    def test_image_attachment_resolves_to_pinned_vision_role(self) -> None:
        """A raster image always resolves via the pinned ``vision`` binding —
        never the calling role, never a profile, never a per-attachment choice.
        """
        ctx = self._make_ctx((self._make_attachment(),))
        models = {
            "primary": self._model_def(supports_vision=False, provider_type="local"),
            "claude_sonnet": self._model_def(supports_vision=True, provider_type="cloud"),
        }
        with self._patch_models(models, vision_deployment="claude_sonnet"):
            assert _resolve_vision_routing_key(ctx, "primary") == "claude_sonnet"

    def test_multiple_image_attachments_still_resolve_to_the_single_pinned_role(self) -> None:
        """No per-attachment override exists anymore — every image in the turn
        is served by the one pinned vision model.
        """
        ctx = self._make_ctx(
            (
                self._make_attachment(artifact_id="a1"),
                self._make_attachment(artifact_id="a2"),
            )
        )
        models = {"claude_sonnet": self._model_def(supports_vision=True, provider_type="cloud")}
        with self._patch_models(models, vision_deployment="claude_sonnet"):
            assert _resolve_vision_routing_key(ctx, "primary") == "claude_sonnet"

    def test_misconfigured_pinned_vision_model_fails_closed(self) -> None:
        """A config-drift guard: if `vision` is ever bound to a non-vision-capable
        deployment, this fails loud rather than silently degrading.
        """
        ctx = self._make_ctx((self._make_attachment(),))
        models = {"claude_sonnet": self._model_def(supports_vision=False, provider_type="cloud")}
        with self._patch_models(models, vision_deployment="claude_sonnet"):
            with pytest.raises(AttachmentUnsupportedError):
                _resolve_vision_routing_key(ctx, "primary")

    def test_vision_role_not_configured_fails_closed(self) -> None:
        """No `vision` binding at all — fails closed rather than falling through
        to some other model.
        """
        ctx = self._make_ctx((self._make_attachment(),))
        models = {"primary": self._model_def(supports_vision=True, provider_type="local")}
        with self._patch_models(models, vision_deployment=None):
            with pytest.raises(AttachmentUnsupportedError):
                _resolve_vision_routing_key(ctx, "primary")


class TestDocumentRouting:
    """ADR-0121 T5 (FRE-920): document routing mirrors ``TestVisionRouting`` —
    the pinned ``vision`` role serves any Tier-2 PDF, choosing native-PDF vs.
    rasterize delivery from its own declared capabilities. No per-attachment
    override, no profile, no escalation logic survives.
    """

    def _make_attachment(self, **overrides: object) -> AttachmentRef:
        defaults: dict[str, object] = {
            "artifact_id": "doc-123",
            "content_type": "application/pdf",
            "title": "report.pdf",
            "r2_key": "upload/user/GLOBAL/report.pdf",
        }
        defaults.update(overrides)
        return AttachmentRef(**defaults)  # type: ignore[arg-type]

    def _make_image_attachment(self, **overrides: object) -> AttachmentRef:
        defaults: dict[str, object] = {
            "artifact_id": "img-123",
            "content_type": "image/png",
            "title": "photo.png",
            "r2_key": "upload/user/GLOBAL/photo.png",
        }
        defaults.update(overrides)
        return AttachmentRef(**defaults)  # type: ignore[arg-type]

    def _make_ctx(self, attachments: tuple[AttachmentRef, ...]) -> ExecutionContext:
        return ExecutionContext(
            session_id="s1",
            trace_id="t1",
            user_message="hello",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            attachments=attachments,
        )

    def _model_def(
        self,
        *,
        supports_vision: bool = False,
        supports_pdf_document: bool = False,
        provider_type: str = "local",
    ) -> ModelDefinition:
        return ModelDefinition(
            id="test-model",
            context_length=8192,
            max_concurrency=1,
            default_timeout=30,
            provider="slm_local" if provider_type == "local" else "anthropic",
            supports_vision=supports_vision,
            supports_pdf_document=supports_pdf_document,
        )

    def _patch_models(
        self, models: dict[str, ModelDefinition], *, vision_deployment: str | None = "claude_sonnet"
    ) -> Any:
        roles = {}
        if vision_deployment is not None:
            roles["vision"] = RoleBinding(deployment=vision_deployment)
        return patch(
            "personal_agent.config.model_loader.load_model_config",
            return_value=ModelConfig(
                providers={
                    "slm_local": ProviderDefinition(placement=Placement.LOCAL, max_concurrency=2),
                    "anthropic": ProviderDefinition(placement=Placement.CLOUD, max_concurrency=50),
                },
                models=models,
                roles=roles,
            ),
        )

    def test_native_pdf_capable_pinned_model_uses_native_pdf(self) -> None:
        ctx = self._make_ctx((self._make_attachment(),))
        models = {"claude_sonnet": self._model_def(supports_pdf_document=True)}
        with self._patch_models(models):
            key, mode = _resolve_document_routing_key(ctx, "primary")
        assert key == "claude_sonnet"
        assert mode == "native_pdf"

    def test_vision_only_pinned_model_falls_back_to_rasterize(self) -> None:
        ctx = self._make_ctx((self._make_attachment(),))
        models = {"claude_sonnet": self._model_def(supports_vision=True)}
        with self._patch_models(models):
            key, mode = _resolve_document_routing_key(ctx, "primary")
        assert key == "claude_sonnet"
        assert mode == "rasterize"

    def test_pinned_model_prefers_native_pdf_when_both_supported(self) -> None:
        ctx = self._make_ctx((self._make_attachment(),))
        models = {
            "claude_sonnet": self._model_def(supports_pdf_document=True, supports_vision=True)
        }
        with self._patch_models(models):
            key, mode = _resolve_document_routing_key(ctx, "primary")
        assert key == "claude_sonnet"
        assert mode == "native_pdf"

    def test_pinned_model_incapable_of_either_fails_closed(self) -> None:
        ctx = self._make_ctx((self._make_attachment(),))
        models = {"claude_sonnet": self._model_def()}
        with self._patch_models(models):
            with pytest.raises(AttachmentUnsupportedError):
                _resolve_document_routing_key(ctx, "primary")

    def test_mixed_image_and_document_requires_vision_even_if_pdf_native_capable(self) -> None:
        """A pinned model with `supports_pdf_document=True` but `supports_vision=False`
        is disqualified when the turn ALSO carries a raster image — the image's
        hard vision requirement applies regardless of the document's own
        native-PDF capability.
        """
        ctx = self._make_ctx((self._make_attachment(), self._make_image_attachment()))
        models = {"claude_sonnet": self._model_def(supports_pdf_document=True)}
        with self._patch_models(models):
            with pytest.raises(AttachmentUnsupportedError):
                _resolve_document_routing_key(ctx, "primary")

    def test_mixed_image_and_document_uses_native_pdf_when_pinned_model_supports_both(
        self,
    ) -> None:
        ctx = self._make_ctx((self._make_attachment(), self._make_image_attachment()))
        models = {
            "claude_sonnet": self._model_def(supports_pdf_document=True, supports_vision=True)
        }
        with self._patch_models(models):
            key, mode = _resolve_document_routing_key(ctx, "primary")
        assert key == "claude_sonnet"
        assert mode == "native_pdf"

    def test_vision_role_not_configured_fails_closed(self) -> None:
        ctx = self._make_ctx((self._make_attachment(),))
        models = {"primary": self._model_def(supports_pdf_document=True)}
        with self._patch_models(models, vision_deployment=None):
            with pytest.raises(AttachmentUnsupportedError):
                _resolve_document_routing_key(ctx, "primary")
