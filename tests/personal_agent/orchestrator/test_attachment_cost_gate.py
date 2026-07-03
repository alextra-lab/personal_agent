"""FRE-691 / ADR-0101 §8b AC-10: pre-flight cloud-attachment cost gate.

Proves the over-threshold turn makes no model call until confirmed, the confirmed
turn proceeds, the under-threshold turn proceeds silently, and — per the codex plan
review — a multi-call turn is confirmed once, a stored "always proceed" preference is
ignored for this cost constraint, and the ADR-0065 reservation for an image turn
covers the image estimate before the call.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from personal_agent.cost_gate import BudgetConfig, CapEntry, OnDenialBehaviour, RoleConfig
from personal_agent.governance.models import Mode
from personal_agent.llm_client.cost_estimator import estimate_reservation_for_call
from personal_agent.llm_client.models import ModelConfig, ModelDefinition
from personal_agent.llm_client.pricing import register_model_pricing
from personal_agent.orchestrator import executor as executor_mod
from personal_agent.orchestrator.attachment_cost import estimate_attachment_cloud_cost_usd
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.types import AttachmentRef, ExecutionContext, TaskState
from personal_agent.telemetry.trace import TraceContext

pytestmark = pytest.mark.asyncio

_IMAGE_BLOCK = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


def _ctx(sm: SessionManager) -> ExecutionContext:
    session_id = sm.create_session(Mode.NORMAL, Channel.CHAT)
    trace = TraceContext.new_trace()
    return ExecutionContext(
        session_id=session_id,
        trace_id=trace.trace_id,
        user_message="what is in this picture?",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        eval_mode=True,
    )


def _cloud_def(input_price: float = 0.000003) -> ModelDefinition:
    return ModelDefinition(
        id="claude-sonnet-4-6",
        provider="anthropic",
        provider_type="cloud",
        max_tokens=32768,
        context_length=200000,
        max_concurrency=10,
        default_timeout=180,
        supports_vision=True,
        input_cost_per_token=input_price,
        output_cost_per_token=0.000015,
    )


def _patch_routing(
    monkeypatch: pytest.MonkeyPatch, model_def: ModelDefinition, key: str = "claude_sonnet"
) -> None:
    monkeypatch.setattr(executor_mod, "_resolve_vision_routing_key", lambda ctx, role: key)
    cfg = ModelConfig(
        models={key: model_def},
        entity_extraction_role=key,
        captains_log_role=key,
        insights_role=key,
    )
    monkeypatch.setattr("personal_agent.config.model_loader.load_model_config", lambda *a, **k: cfg)


async def test_over_threshold_stops_with_prompt_and_no_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-10(a): over-threshold + keep_local default → no model call, reply carries $ + prompt."""
    sm = SessionManager()
    ctx = _ctx(sm)
    # Absurd price so a single image trips the $0.50 default threshold.
    _patch_routing(monkeypatch, _cloud_def(input_price=0.001))  # 1 × 1600 × 0.001 = $1.60
    # No WS waiter → _maybe_pause_for_constraint returns the safe default keep_local.
    monkeypatch.setattr(
        executor_mod, "_maybe_pause_for_constraint", AsyncMock(return_value="keep_local")
    )
    # Keep the unit test hermetic — the keep_local path persists pending state to
    # Postgres; stub the durable save so no DB is required here (FRE-749).
    monkeypatch.setattr(executor_mod, "_save_pending_cloud_confirmation", AsyncMock())

    proceed = await executor_mod._maybe_confirm_attachment_cost(ctx, [_IMAGE_BLOCK])

    assert proceed is False
    assert ctx.attachment_cost_confirmed is False
    assert "1.6" in ctx.final_reply  # the dollar estimate is disclosed
    assert "proceed" in ctx.final_reply.lower() and "local" in ctx.final_reply.lower()


async def test_step_init_short_circuits_to_synthesis(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-10(a): when the gate says stop, step_init returns SYNTHESIS — LLM_CALL skipped."""
    sm = SessionManager()
    ctx = _ctx(sm)
    ctx.attachments = (
        AttachmentRef(artifact_id="a1", content_type="image/png", title="pic.png", r2_key="k1"),
    )
    # Skip real R2 fetch: hand step_init a resolved image block directly. step_init
    # imports resolve_attachments locally, so patch it at its source module.
    from personal_agent.orchestrator.attachment_resolution import ResolvedAttachments

    monkeypatch.setattr(
        "personal_agent.orchestrator.attachment_resolution.resolve_attachments",
        AsyncMock(return_value=ResolvedAttachments(blocks=(_IMAGE_BLOCK,), disclosures=())),
    )
    monkeypatch.setattr(
        executor_mod, "_maybe_confirm_attachment_cost", AsyncMock(return_value=False)
    )

    next_state = await executor_mod.step_init(ctx, sm, TraceContext.new_trace())

    assert next_state == TaskState.SYNTHESIS


async def test_confirm_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-10(b): a proceed_cloud decision continues the turn and marks it confirmed."""
    sm = SessionManager()
    ctx = _ctx(sm)
    _patch_routing(monkeypatch, _cloud_def(input_price=0.001))
    monkeypatch.setattr(
        executor_mod, "_maybe_pause_for_constraint", AsyncMock(return_value="proceed_cloud")
    )

    proceed = await executor_mod._maybe_confirm_attachment_cost(ctx, [_IMAGE_BLOCK])

    assert proceed is True
    assert ctx.attachment_cost_confirmed is True


async def test_under_threshold_proceeds_without_pausing(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-10(c) precondition: a cheap image turn proceeds silently (no confirm)."""
    sm = SessionManager()
    ctx = _ctx(sm)
    _patch_routing(monkeypatch, _cloud_def(input_price=0.000003))  # 1 image ≈ $0.0048 < $0.50
    pause = AsyncMock(return_value="keep_local")
    monkeypatch.setattr(executor_mod, "_maybe_pause_for_constraint", pause)

    proceed = await executor_mod._maybe_confirm_attachment_cost(ctx, [_IMAGE_BLOCK])

    assert proceed is True
    assert ctx.attachment_cost_confirmed is True
    pause.assert_not_awaited()


async def test_local_routing_is_free_and_ungated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local (free) routing target never triggers the cost gate."""
    sm = SessionManager()
    ctx = _ctx(sm)
    local_def = ModelDefinition(
        id="qwen-local",
        provider=None,
        provider_type="local",
        context_length=40000,
        max_concurrency=1,
        default_timeout=120,
        supports_vision=True,
    )
    _patch_routing(monkeypatch, local_def, key="primary")
    pause = AsyncMock(return_value="keep_local")
    monkeypatch.setattr(executor_mod, "_maybe_pause_for_constraint", pause)

    proceed = await executor_mod._maybe_confirm_attachment_cost(ctx, [_IMAGE_BLOCK])

    assert proceed is True
    pause.assert_not_awaited()


async def test_multi_call_turn_confirmed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex High-1: one confirmation flags the whole turn (re-entry never re-prompts)."""
    sm = SessionManager()
    ctx = _ctx(sm)
    _patch_routing(monkeypatch, _cloud_def(input_price=0.001))
    pause = AsyncMock(return_value="proceed_cloud")
    monkeypatch.setattr(executor_mod, "_maybe_pause_for_constraint", pause)

    await executor_mod._maybe_confirm_attachment_cost(ctx, [_IMAGE_BLOCK, _IMAGE_BLOCK])

    # The per-turn flag is set; the gate lives only in step_init (once/turn), so a
    # subsequent LLM_CALL re-entry with the images still in context never re-prompts.
    assert ctx.attachment_cost_confirmed is True
    pause.assert_awaited_once()


async def test_cost_constraint_ignores_stored_preference(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex Medium-3: the cost pause is called with allow_preference=False (never silent spend)."""
    sm = SessionManager()
    ctx = _ctx(sm)
    _patch_routing(monkeypatch, _cloud_def(input_price=0.001))
    pause = AsyncMock(return_value="keep_local")
    monkeypatch.setattr(executor_mod, "_maybe_pause_for_constraint", pause)
    monkeypatch.setattr(executor_mod, "_save_pending_cloud_confirmation", AsyncMock())

    await executor_mod._maybe_confirm_attachment_cost(ctx, [_IMAGE_BLOCK])

    assert pause.await_args.kwargs["allow_preference"] is False
    assert pause.await_args.kwargs["constraint"] == "attachment_cost"


async def test_reservation_covers_image_estimate() -> None:
    """AC-10(c): the ADR-0065 reservation for an image turn ≥ the image estimate.

    The reservation is sized from ``token_counter`` over the full message content,
    which counts the image block — so it is non-blank and includes the image basis,
    not text-only. (The reservation is recorded before the call by the already-tested
    litellm_client reserve→respond path.)
    """
    register_model_pricing(
        ModelConfig(
            models={"claude_sonnet": _cloud_def(input_price=0.000003)},
            entity_extraction_role="claude_sonnet",
            captains_log_role="claude_sonnet",
            insights_role="claude_sonnet",
        )
    )
    budget = BudgetConfig(
        version=1,
        roles={
            "main_inference": RoleConfig(
                default_output_tokens=1024,
                safety_factor=1.2,
                on_denial=OnDenialBehaviour.RAISE,
            )
        },
        caps=[CapEntry(time_window="weekly", role="_total", cap_usd=Decimal("25.00"))],
    )
    image_message = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this?"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": (
                            "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1"
                            "HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
                        )
                    },
                },
            ],
        }
    ]
    reservation = estimate_reservation_for_call(
        role="main_inference",
        model="anthropic/claude-sonnet-4-6",
        messages=image_message,
        max_tokens=1024,
        config=budget,
    )
    # A single-image estimate at the same price; the reservation must cover it.
    image_estimate = estimate_attachment_cloud_cost_usd(
        block_count=1, per_block_tokens=1600, input_price_per_token=Decimal("0.000003")
    )
    assert reservation > 0
    assert reservation >= image_estimate
