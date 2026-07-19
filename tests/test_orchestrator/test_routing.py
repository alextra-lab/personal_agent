"""Tests for routing: heuristic classification + two-tier model taxonomy (ADR-0033)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.config import settings
from personal_agent.config.profile import DelegationConfig, ExecutionProfile, set_current_profile
from personal_agent.exceptions import AttachmentUnsupportedError
from personal_agent.governance.models import Mode
from personal_agent.llm_client import ModelRole
from personal_agent.llm_client.models import (
    ModelConfig,
    ModelDefinition,
    Placement,
    ProviderDefinition,
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
    """AC-4 (capability assert + escalate + fail-closed) and AC-9 (processing_target
    override, "local" wins on conflict) — ADR-0101 §5, §8a.
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

    def _patch_models(self, models: dict[str, ModelDefinition]) -> Any:
        """Patch in a REAL ModelConfig, not a MagicMock.

        A MagicMock's `placement_of` returns a MagicMock, which is never
        `Placement.LOCAL` — so every deployment would silently read as cloud and
        the local-routing assertions would pass or fail for the wrong reason.
        Building the real object makes placement resolution genuine.
        """
        # No Layer-3 bindings in these fixtures: they key their models by ROLE
        # name, so resolution must fall back to the role-as-key path rather than
        # dereference a binding (ADR-0121).
        return patch(
            "personal_agent.config.model_loader.load_model_config",
            return_value=ModelConfig(
                providers={
                    "slm_local": ProviderDefinition(
                        placement=Placement.LOCAL, max_concurrency=2
                    ),
                    "anthropic": ProviderDefinition(
                        placement=Placement.CLOUD, max_concurrency=50
                    ),
                },
                models=models,
                roles={},
            ),
        )

    def test_no_image_attachment_is_noop(self) -> None:
        """No raster image attachment — returns resolve_model_key(role_name) unchanged."""
        ctx = self._make_ctx(())
        assert _resolve_vision_routing_key(ctx, "primary") == "qwen3.6-35b-thinking"

    def test_non_raster_attachment_is_noop(self) -> None:
        """A PDF attachment (ADR-0102 territory) never triggers vision routing."""
        ctx = self._make_ctx((self._make_attachment(content_type="application/pdf"),))
        assert _resolve_vision_routing_key(ctx, "primary") == "qwen3.6-35b-thinking"

    def test_ac4_capable_primary_no_override_proceeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC5 flip-back: with attachment_default_processing_target='local', Auto restores
        the pre-FRE-886 behavior — primary already supports vision, no escalation needed.
        """
        monkeypatch.setattr(settings, "attachment_default_processing_target", "local")
        ctx = self._make_ctx((self._make_attachment(),))
        assert _resolve_vision_routing_key(ctx, "primary") == "qwen3.6-35b-thinking"

    def test_default_cloud_no_override_routes_to_escalation_model_image(self) -> None:
        """FRE-886 AC1: default config ('cloud') routes Auto straight to the escalation
        model even though the local primary is perfectly vision-capable.
        """
        ctx = self._make_ctx((self._make_attachment(),))
        models = {
            "primary": self._model_def(supports_vision=True, provider_type="local"),
            "claude_sonnet": self._model_def(supports_vision=True, provider_type="cloud"),
        }
        profile = ExecutionProfile(
            name="local",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="local",
            delegation=DelegationConfig(
                allow_cloud_escalation=False,
                escalation_provider="anthropic",
                escalation_model="claude_sonnet",
            ),
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                assert _resolve_vision_routing_key(ctx, "primary") == "claude_sonnet"
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    def test_default_cloud_no_override_fails_closed_without_escalation_model(self) -> None:
        """FRE-886: default config ('cloud') with no escalation_model configured fails
        closed rather than silently falling back to a capable local model.
        """
        ctx = self._make_ctx((self._make_attachment(),))
        models = {"primary": self._model_def(supports_vision=True, provider_type="local")}
        profile = ExecutionProfile(
            name="local",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="local",
            delegation=DelegationConfig(allow_cloud_escalation=False),
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                with pytest.raises(AttachmentUnsupportedError):
                    _resolve_vision_routing_key(ctx, "primary")
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    def test_default_cloud_no_profile_bound_falls_back_to_profile_independent_resolution(
        self,
    ) -> None:
        """FRE-886 code-review fix: with no ExecutionProfile bound at all (e.g. a
        failed load_profile in service/app.py), Auto must NOT hard-fail — there is
        no "profile's escalation model" to route to, so it falls through to the
        pre-FRE-886 profile-independent resolution instead of regressing a turn
        that previously succeeded.
        """
        ctx = self._make_ctx((self._make_attachment(),))
        models = {"primary": self._model_def(supports_vision=True, provider_type="local")}
        with self._patch_models(models):
            assert _resolve_vision_routing_key(ctx, "primary") == "primary"

    def test_ac4_incapable_primary_escalation_permitted_escalates(self) -> None:
        """Non-vision primary + escalation-permitted profile → escalates to the capable model."""
        ctx = self._make_ctx((self._make_attachment(),))
        models = {
            "primary": self._model_def(supports_vision=False, provider_type="local"),
            "claude_sonnet": self._model_def(supports_vision=True, provider_type="cloud"),
        }
        profile = ExecutionProfile(
            name="cloud",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="cloud",
            delegation=DelegationConfig(
                allow_cloud_escalation=True,
                escalation_provider="anthropic",
                escalation_model="claude_sonnet",
            ),
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                assert _resolve_vision_routing_key(ctx, "primary") == "claude_sonnet"
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    def test_ac4_incapable_primary_escalation_forbidden_raises(self) -> None:
        """Non-vision primary + no escalation permitted → fails closed, never silent."""
        ctx = self._make_ctx((self._make_attachment(),))
        models = {"primary": self._model_def(supports_vision=False, provider_type="local")}
        profile = ExecutionProfile(
            name="local",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="local",
            delegation=DelegationConfig(allow_cloud_escalation=False),
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                with pytest.raises(AttachmentUnsupportedError):
                    _resolve_vision_routing_key(ctx, "primary")
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    def test_ac9_local_override_never_escalates_even_under_cloud_profile(self) -> None:
        """ "local" override stays pinned to the raw role's own local deployment — never
        resolves via the active (cloud) profile's redirect, and fails closed rather than
        silently escalating when that local model isn't vision-capable.
        """
        ctx = self._make_ctx((self._make_attachment(processing_target="local"),))
        models = {
            "primary": self._model_def(supports_vision=False, provider_type="local"),
            "claude_sonnet": self._model_def(supports_vision=True, provider_type="cloud"),
        }
        profile = ExecutionProfile(
            name="cloud",
            primary_model="claude_sonnet",  # profile redirects "primary" -> claude_sonnet
            sub_agent_model="claude_haiku",
            provider_type="cloud",
            delegation=DelegationConfig(
                allow_cloud_escalation=True,
                escalation_provider="anthropic",
                escalation_model="claude_sonnet",
            ),
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                with pytest.raises(AttachmentUnsupportedError):
                    _resolve_vision_routing_key(ctx, "primary")
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    def test_ac9_local_override_succeeds_when_local_model_capable(self) -> None:
        """ "local" override succeeds on the raw local model when it is vision-capable,
        even under an active cloud profile.
        """
        ctx = self._make_ctx((self._make_attachment(processing_target="local"),))
        models = {
            "primary": self._model_def(supports_vision=True, provider_type="local"),
            "claude_sonnet": self._model_def(supports_vision=True, provider_type="cloud"),
        }
        profile = ExecutionProfile(
            name="cloud",
            primary_model="claude_sonnet",
            sub_agent_model="claude_haiku",
            provider_type="cloud",
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                assert _resolve_vision_routing_key(ctx, "primary") == "primary"
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    def test_ac9_cloud_override_forces_cloud_even_on_local_profile(self) -> None:
        """ "cloud" override forces the cloud vision path from a local-profile conversation,
        even though that profile's allow_cloud_escalation is False.
        """
        ctx = self._make_ctx((self._make_attachment(processing_target="cloud"),))
        models = {
            "primary": self._model_def(supports_vision=True, provider_type="local"),
            "claude_sonnet": self._model_def(supports_vision=True, provider_type="cloud"),
        }
        profile = ExecutionProfile(
            name="local",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="local",
            delegation=DelegationConfig(
                allow_cloud_escalation=False,
                escalation_provider="anthropic",
                escalation_model="claude_sonnet",
            ),
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                assert _resolve_vision_routing_key(ctx, "primary") == "claude_sonnet"
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    def test_ac9_cloud_override_fails_closed_when_no_escalation_model_configured(self) -> None:
        """ "cloud" override with no escalation_model configured fails closed."""
        ctx = self._make_ctx((self._make_attachment(processing_target="cloud"),))
        models = {"primary": self._model_def(supports_vision=True, provider_type="local")}
        profile = ExecutionProfile(
            name="local",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="local",
            delegation=DelegationConfig(allow_cloud_escalation=False),
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                with pytest.raises(AttachmentUnsupportedError):
                    _resolve_vision_routing_key(ctx, "primary")
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    def test_conflicting_overrides_local_wins(self) -> None:
        """One attachment "local", another "cloud" in the same turn — "local" wins (never
        escalates) even though a vision-capable cloud escalation model IS configured.
        """
        ctx = self._make_ctx(
            (
                self._make_attachment(artifact_id="a1", processing_target="local"),
                self._make_attachment(artifact_id="a2", processing_target="cloud"),
            )
        )
        models = {
            "primary": self._model_def(supports_vision=False, provider_type="local"),
            "claude_sonnet": self._model_def(supports_vision=True, provider_type="cloud"),
        }
        profile = ExecutionProfile(
            name="local",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="local",
            delegation=DelegationConfig(
                allow_cloud_escalation=False,
                escalation_provider="anthropic",
                escalation_model="claude_sonnet",
            ),
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                # local wins -> primary isn't vision-capable in this mock -> fail closed,
                # NOT silently escalated to claude_sonnet despite it being available.
                with pytest.raises(AttachmentUnsupportedError):
                    _resolve_vision_routing_key(ctx, "primary")
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)


class TestDocumentRouting:
    """AC-6 (fail-closed by capability, escalation target included) and AC-8 backend

    (per-attachment override honored, fail-closed) — ADR-0102 §3, FRE-684. Mirrors
    ``TestVisionRouting``'s exact helper conventions; ``_resolve_vision_routing_key``
    itself is untouched by this ticket (proven separately above).
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
            provider_type=provider_type,
            supports_vision=supports_vision,
            supports_pdf_document=supports_pdf_document,
        )

    def _patch_models(self, models: dict[str, ModelDefinition]) -> Any:
        mock_config = MagicMock()
        mock_config.models = models
        # No Layer-3 bindings in these fixtures: they key their models by ROLE
        # name, so resolution must fall back to the role-as-key path rather than
        # dereference a MagicMock binding (ADR-0121).
        mock_config.roles = {}
        return patch(
            "personal_agent.config.model_loader.load_model_config", return_value=mock_config
        )

    # --- AC-6: fail-closed by capability, escalation target included ---

    def test_ac6_native_pdf_capable_primary_no_override_proceeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC5 flip-back: with attachment_default_processing_target='local'."""
        monkeypatch.setattr(settings, "attachment_default_processing_target", "local")
        ctx = self._make_ctx((self._make_attachment(),))
        models = {"primary": self._model_def(supports_pdf_document=True, provider_type="local")}
        profile = ExecutionProfile(
            name="local",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="local",
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                key, mode = _resolve_document_routing_key(ctx, "primary")
            assert key == "primary"
            assert mode == "native_pdf"
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    def test_ac6_vision_only_primary_falls_back_to_rasterize(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC5 flip-back: with attachment_default_processing_target='local'."""
        monkeypatch.setattr(settings, "attachment_default_processing_target", "local")
        ctx = self._make_ctx((self._make_attachment(),))
        models = {"primary": self._model_def(supports_vision=True, provider_type="local")}
        profile = ExecutionProfile(
            name="local",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="local",
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                key, mode = _resolve_document_routing_key(ctx, "primary")
            assert key == "primary"
            assert mode == "rasterize"
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    def test_default_cloud_no_override_routes_to_escalation_model_native_pdf(self) -> None:
        """FRE-886 AC2: default config ('cloud') routes an Auto PDF straight to the
        escalation model via the native PDF document block, even though the local
        primary is perfectly capable.
        """
        ctx = self._make_ctx((self._make_attachment(),))
        models = {
            "primary": self._model_def(supports_pdf_document=True, provider_type="local"),
            "claude_sonnet": self._model_def(supports_pdf_document=True, provider_type="cloud"),
        }
        profile = ExecutionProfile(
            name="local",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="local",
            delegation=DelegationConfig(
                allow_cloud_escalation=False,
                escalation_provider="anthropic",
                escalation_model="claude_sonnet",
            ),
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                key, mode = _resolve_document_routing_key(ctx, "primary")
            assert key == "claude_sonnet"
            assert mode == "native_pdf"
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    def test_default_cloud_no_profile_bound_falls_back_to_profile_independent_resolution(
        self,
    ) -> None:
        """FRE-886 code-review fix: with no ExecutionProfile bound, an Auto PDF must
        NOT hard-fail — falls through to the pre-FRE-886 profile-independent
        resolution instead of regressing a turn that previously succeeded.
        """
        ctx = self._make_ctx((self._make_attachment(),))
        models = {"primary": self._model_def(supports_pdf_document=True, provider_type="local")}
        with self._patch_models(models):
            key, mode = _resolve_document_routing_key(ctx, "primary")
        assert key == "primary"
        assert mode == "native_pdf"

    def test_default_cloud_no_override_mixed_image_and_document_routes_to_escalation_model(
        self,
    ) -> None:
        """FRE-886: default config ('cloud') routes an Auto turn carrying both a PDF and
        an image to the escalation model, honoring the combined vision+native-PDF
        capability predicate.
        """
        ctx = self._make_ctx((self._make_attachment(), self._make_image_attachment()))
        models = {
            "primary": self._model_def(
                supports_pdf_document=True, supports_vision=True, provider_type="local"
            ),
            "claude_sonnet": self._model_def(
                supports_pdf_document=True, supports_vision=True, provider_type="cloud"
            ),
        }
        profile = ExecutionProfile(
            name="local",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="local",
            delegation=DelegationConfig(
                allow_cloud_escalation=False,
                escalation_provider="anthropic",
                escalation_model="claude_sonnet",
            ),
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                key, mode = _resolve_document_routing_key(ctx, "primary")
            assert key == "claude_sonnet"
            assert mode == "native_pdf"
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    def test_ac6_incapable_primary_escalation_permitted_escalates_to_native_pdf(self) -> None:
        ctx = self._make_ctx((self._make_attachment(),))
        models = {
            "primary": self._model_def(provider_type="local"),
            "claude_sonnet": self._model_def(supports_pdf_document=True, provider_type="cloud"),
        }
        profile = ExecutionProfile(
            name="cloud",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="cloud",
            delegation=DelegationConfig(
                allow_cloud_escalation=True,
                escalation_provider="anthropic",
                escalation_model="claude_sonnet",
            ),
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                key, mode = _resolve_document_routing_key(ctx, "primary")
            assert key == "claude_sonnet"
            assert mode == "native_pdf"
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    def test_ac6_primary_and_escalation_both_non_capable_fails_closed(self) -> None:
        """The literal AC-6 case."""
        ctx = self._make_ctx((self._make_attachment(),))
        models = {
            "primary": self._model_def(provider_type="local"),
            "claude_sonnet": self._model_def(provider_type="cloud"),
        }
        profile = ExecutionProfile(
            name="cloud",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="cloud",
            delegation=DelegationConfig(
                allow_cloud_escalation=True,
                escalation_provider="anthropic",
                escalation_model="claude_sonnet",
            ),
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                with pytest.raises(AttachmentUnsupportedError):
                    _resolve_document_routing_key(ctx, "primary")
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    # --- AC-8 (backend): per-attachment override honored, fail-closed ---

    def test_ac8_local_override_never_escalates_even_under_cloud_profile(self) -> None:
        ctx = self._make_ctx((self._make_attachment(processing_target="local"),))
        models = {
            "primary": self._model_def(provider_type="local"),
            "claude_sonnet": self._model_def(supports_pdf_document=True, provider_type="cloud"),
        }
        profile = ExecutionProfile(
            name="cloud",
            primary_model="claude_sonnet",
            sub_agent_model="claude_haiku",
            provider_type="cloud",
            delegation=DelegationConfig(
                allow_cloud_escalation=True,
                escalation_provider="anthropic",
                escalation_model="claude_sonnet",
            ),
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                with pytest.raises(AttachmentUnsupportedError):
                    _resolve_document_routing_key(ctx, "primary")
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    def test_ac8_cloud_override_forces_native_pdf_even_on_local_profile(self) -> None:
        ctx = self._make_ctx((self._make_attachment(processing_target="cloud"),))
        models = {
            "primary": self._model_def(supports_vision=True, provider_type="local"),
            "claude_sonnet": self._model_def(supports_pdf_document=True, provider_type="cloud"),
        }
        profile = ExecutionProfile(
            name="local",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="local",
            delegation=DelegationConfig(
                allow_cloud_escalation=False,
                escalation_provider="anthropic",
                escalation_model="claude_sonnet",
            ),
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                key, mode = _resolve_document_routing_key(ctx, "primary")
            assert key == "claude_sonnet"
            assert mode == "native_pdf"
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    def test_ac8_cloud_override_fails_closed_when_no_escalation_model_configured(self) -> None:
        ctx = self._make_ctx((self._make_attachment(processing_target="cloud"),))
        models = {"primary": self._model_def(supports_vision=True, provider_type="local")}
        profile = ExecutionProfile(
            name="local",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="local",
            delegation=DelegationConfig(allow_cloud_escalation=False),
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                with pytest.raises(AttachmentUnsupportedError):
                    _resolve_document_routing_key(ctx, "primary")
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    # --- Mixed image + document turn: combined capability predicate ---

    def test_mixed_image_and_document_requires_vision_even_if_pdf_native_capable(self) -> None:
        """A model with supports_pdf_document=True but supports_vision=False is

        disqualified when the turn ALSO carries a raster image — the image's
        hard vision requirement applies regardless of the document's own
        native-PDF capability.
        """
        ctx = self._make_ctx((self._make_attachment(), self._make_image_attachment()))
        models = {
            "primary": self._model_def(supports_pdf_document=True, provider_type="local"),
            "claude_sonnet": self._model_def(
                supports_pdf_document=True, supports_vision=True, provider_type="cloud"
            ),
        }
        profile = ExecutionProfile(
            name="cloud",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="cloud",
            delegation=DelegationConfig(
                allow_cloud_escalation=True,
                escalation_provider="anthropic",
                escalation_model="claude_sonnet",
            ),
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                key, mode = _resolve_document_routing_key(ctx, "primary")
            assert key == "claude_sonnet"
            assert mode == "native_pdf"
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)

    def test_mixed_image_and_document_prefers_native_pdf_when_both_supported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC5 flip-back: with attachment_default_processing_target='local'."""
        monkeypatch.setattr(settings, "attachment_default_processing_target", "local")
        ctx = self._make_ctx((self._make_attachment(), self._make_image_attachment()))
        models = {
            "primary": self._model_def(
                supports_pdf_document=True, supports_vision=True, provider_type="local"
            )
        }
        profile = ExecutionProfile(
            name="local",
            primary_model="primary",
            sub_agent_model="sub_agent",
            provider_type="local",
        )
        token = set_current_profile(profile)
        try:
            with self._patch_models(models):
                key, mode = _resolve_document_routing_key(ctx, "primary")
            assert key == "primary"
            assert mode == "native_pdf"
        finally:
            from personal_agent.config.profile import _current_profile

            _current_profile.reset(token)
