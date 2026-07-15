"""Content-widening regression tests (ADR-0101 §2, FRE-664).

Proves every audited orchestrator/context_window site degrades safely when a
message ``content`` field is a list of typed blocks (text + image) instead of a
plain string — the shape ticket 4 will start producing for real image
attachments. No real image is resolved here; these tests only prove the
pipeline no longer corrupts, drops, or crashes on block-list content (AC-3
slice).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.llm_client.message_content import get_text_content
from personal_agent.orchestrator.context_window import estimate_message_tokens
from personal_agent.orchestrator.executor import (
    _append_no_think_synthesis_nudge,
    _append_no_think_to_last_user_message,
    _inline_volatile_into_last_user_message,
    _validate_and_fix_conversation_roles,
)
from personal_agent.service.models import Message

_TEXT_BLOCK = {"type": "text", "text": "look at this"}
_IMAGE_BLOCK = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
_BLOCK_LIST = [_TEXT_BLOCK, _IMAGE_BLOCK]


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals() -> object:
    """Restore executor's lazily-cached registry globals after each test.

    The AC-3 test below patches ``executor.get_default_registry`` and drives
    ``step_llm_call``, which seeds the module-level ``_tool_registry`` /
    ``_tool_execution_layer`` from the *patched* (empty) registry. Without this
    restore the patched registry leaks past the patch scope and pollutes later
    tests in the process (mirrors the identical fixture in
    ``test_skill_injection.py``).
    """
    import personal_agent.orchestrator.executor as _ex

    saved_registry = _ex._tool_registry
    saved_layer = _ex._tool_execution_layer
    yield
    _ex._tool_registry = saved_registry
    _ex._tool_execution_layer = saved_layer


# ---------------------------------------------------------------------------
# 3a. Duplicate-role merge — block list preserved through merge, not corrupted
# ---------------------------------------------------------------------------


def test_duplicate_role_merge_preserves_block_list() -> None:
    history = [
        {"role": "user", "content": "first turn"},
        {"role": "user", "content": _BLOCK_LIST},
    ]
    out = _validate_and_fix_conversation_roles(history)

    assert len(out) == 1
    merged = out[0]["content"]
    assert isinstance(merged, list)
    assert {"type": "text", "text": "first turn"} in merged
    assert _TEXT_BLOCK in merged
    assert _IMAGE_BLOCK in merged


def test_duplicate_role_merge_str_only_unchanged_behavior() -> None:
    """Historical str+str merge behavior is preserved (no regression)."""
    history = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]
    out = _validate_and_fix_conversation_roles(history)
    assert out[0]["content"] == "first\n\nsecond"


# ---------------------------------------------------------------------------
# 3b. No-think tool-prompt injection — bug fix: no fallthrough to an older turn
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_think_enabled_local(monkeypatch: pytest.MonkeyPatch):
    from personal_agent.config import settings
    from personal_agent.config.profile import _current_profile, load_profile, set_current_profile

    monkeypatch.setattr(settings, "llm_append_no_think_to_tool_prompts", True)
    token = set_current_profile(load_profile("local"))
    yield
    _current_profile.reset(token)


def test_no_think_skips_block_list_last_user_message(_no_think_enabled_local: None) -> None:
    """Block-list content on the last user turn: no suffix, no corruption."""
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "user", "content": _BLOCK_LIST},
    ]
    out = _append_no_think_to_last_user_message(messages)
    assert out[-1]["content"] == _BLOCK_LIST


def test_no_think_does_not_fall_through_to_older_user_message(
    _no_think_enabled_local: None,
) -> None:
    """Regression: previously `continue` fell through and misapplied the
    suffix to an OLDER user message when the last user turn had list content.
    """
    messages = [
        {"role": "user", "content": "earlier turn"},
        {"role": "assistant", "content": "some reply"},
        {"role": "user", "content": _BLOCK_LIST},
    ]
    out = _append_no_think_to_last_user_message(messages)
    assert out[0]["content"] == "earlier turn"
    assert "/no_think" not in out[0]["content"]
    assert out[-1]["content"] == _BLOCK_LIST


# ---------------------------------------------------------------------------
# 3c. Synthesis nudge — already safe, regression-pin it
# ---------------------------------------------------------------------------


def test_synthesis_nudge_skips_block_list_last_message(_no_think_enabled_local: None) -> None:
    messages = [{"role": "user", "content": _BLOCK_LIST}]
    out = _append_no_think_synthesis_nudge(messages)
    assert out[-1]["content"] == _BLOCK_LIST


# ---------------------------------------------------------------------------
# 3d. Frozen volatile-context inlining — already safe, regression-pin it
# ---------------------------------------------------------------------------


def test_volatile_inlining_skips_block_list_last_user_message() -> None:
    messages = [{"role": "user", "content": _BLOCK_LIST}]
    out = _inline_volatile_into_last_user_message(messages, "some recalled memory")
    assert out == messages
    assert out[-1]["content"] == _BLOCK_LIST


# ---------------------------------------------------------------------------
# 3e/3f. get_text_content at the expansion-query / skill-routing call sites
# ---------------------------------------------------------------------------


def test_get_text_content_extracts_text_not_full_repr() -> None:
    result = get_text_content(_BLOCK_LIST)
    assert result == "look at this"
    assert "image_url" not in result
    assert "AAAA" not in result


# ---------------------------------------------------------------------------
# 4. context_window token estimator — counts image tokens, doesn't stringify
# ---------------------------------------------------------------------------


def test_estimate_message_tokens_counts_image_blocks_not_repr() -> None:
    from personal_agent.llm_client.message_content import IMAGE_BLOCK_TOKEN_ESTIMATE

    text_message = {"role": "user", "content": "look at this"}
    block_message = {"role": "user", "content": _BLOCK_LIST}

    text_tokens = estimate_message_tokens(text_message)
    block_tokens = estimate_message_tokens(block_message)

    assert block_tokens == max(1, text_tokens + IMAGE_BLOCK_TOKEN_ESTIMATE)


def test_estimate_message_tokens_not_inflated_by_huge_base64() -> None:
    huge_uri = "data:image/png;base64," + ("A" * 200_000)
    block_message = {
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": huge_uri}}],
    }
    from personal_agent.llm_client.message_content import IMAGE_BLOCK_TOKEN_ESTIMATE

    assert estimate_message_tokens(block_message) == IMAGE_BLOCK_TOKEN_ESTIMATE


# ---------------------------------------------------------------------------
# 2. service.models.Message — widened type accepts + round-trips block lists
# ---------------------------------------------------------------------------


def test_message_model_accepts_block_list_content() -> None:
    msg = Message(role="user", content=_BLOCK_LIST)
    assert msg.content == _BLOCK_LIST
    assert msg.model_dump()["content"] == _BLOCK_LIST


def test_message_model_still_accepts_str_content() -> None:
    msg = Message(role="user", content="hello")
    assert msg.content == "hello"


# ---------------------------------------------------------------------------
# AC-3 — assembled request_messages carries block-list content intact through
# the real step_llm_call pipeline (no-think injection + role validation) up to
# the llm_client.respond() call boundary.
# ---------------------------------------------------------------------------


def _make_minimal_ctx_with_block_content() -> object:
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.types import ExecutionContext

    return ExecutionContext(
        session_id="test-session",
        trace_id="test-trace",
        user_message="look at this",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=[{"role": "user", "content": _BLOCK_LIST}],
    )


def _make_minimal_response() -> dict[str, object]:
    return {
        "content": "I see the marker.",
        "tool_calls": [],
        "response_id": None,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _make_mock_llm_client(response: dict[str, object]) -> MagicMock:
    mock_client = MagicMock()
    mock_client.respond = AsyncMock(return_value=response)
    mock_client.model_configs = {}
    return mock_client


@pytest.mark.asyncio
async def test_ac3_assembled_request_messages_preserve_image_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from personal_agent.config import settings
    from personal_agent.telemetry.trace import TraceContext

    monkeypatch.setattr(settings, "skill_routing_mode", "hybrid")
    monkeypatch.setattr(settings, "skill_routing_model_key", "")

    ctx = _make_minimal_ctx_with_block_content()
    trace_ctx = TraceContext.new_trace()
    mock_llm = _make_mock_llm_client(_make_minimal_response())
    mock_session = MagicMock()
    mock_session.add_message = AsyncMock()
    mock_session.get_messages = AsyncMock(return_value=[])

    with (
        patch("personal_agent.orchestrator.skills.get_skill_block", return_value=""),
        patch("personal_agent.orchestrator.skills.assemble_skill_index", return_value=""),
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_index_directive",
            return_value="",
        ),
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_usage_directives",
            return_value="",
        ),
        patch("personal_agent.orchestrator.skills.get_all_skills", return_value={}),
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_llm),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        ),
    ):
        from personal_agent.orchestrator.executor import step_llm_call

        await step_llm_call(ctx, mock_session, trace_ctx)  # type: ignore[arg-type]

    assert mock_llm.respond.called, "LLM client was not called"
    request_messages = mock_llm.respond.call_args.kwargs["messages"]

    user_messages = [m for m in request_messages if m.get("role") == "user"]
    assert user_messages, "no user message in assembled request_messages"
    last_user_content = user_messages[-1]["content"]

    assert isinstance(last_user_content, list), (
        f"expected list content on the assembled user turn, got {type(last_user_content)}: "
        f"{last_user_content!r}"
    )
    assert _IMAGE_BLOCK in last_user_content
    assert _TEXT_BLOCK in last_user_content


@pytest.mark.asyncio
async def test_vision_routing_decision_log_fires_for_raster_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-693 (ADR-0074 §8c): step_llm_call logs the routing decision with identity.

    No profile is bound here — the FRE-886 default-cloud fold-in requires a bound
    profile (there is no "profile's escalation model" to route to otherwise), so
    this exercises the pre-FRE-886 profile-independent resolution path regardless
    of attachment_default_processing_target.
    """
    import structlog

    from personal_agent.config import settings
    from personal_agent.orchestrator.types import AttachmentRef
    from personal_agent.telemetry.trace import TraceContext

    monkeypatch.setattr(settings, "skill_routing_mode", "hybrid")
    monkeypatch.setattr(settings, "skill_routing_model_key", "")

    ctx = _make_minimal_ctx_with_block_content()
    ctx.attachments = (
        AttachmentRef(
            artifact_id="abc-123",
            content_type="image/png",
            title="photo.png",
            r2_key="upload/user/GLOBAL/abc.png",
        ),
    )
    trace_ctx = TraceContext.new_trace()
    mock_llm = _make_mock_llm_client(_make_minimal_response())
    mock_session = MagicMock()
    mock_session.add_message = AsyncMock()
    mock_session.get_messages = AsyncMock(return_value=[])

    with (
        patch("personal_agent.orchestrator.skills.get_skill_block", return_value=""),
        patch("personal_agent.orchestrator.skills.assemble_skill_index", return_value=""),
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_index_directive",
            return_value="",
        ),
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_usage_directives",
            return_value="",
        ),
        patch("personal_agent.orchestrator.skills.get_all_skills", return_value={}),
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_llm),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        ),
        structlog.testing.capture_logs() as logs,
    ):
        from personal_agent.orchestrator.executor import step_llm_call

        await step_llm_call(ctx, mock_session, trace_ctx)  # type: ignore[arg-type]

    routed = [e for e in logs if e.get("event") == "vision_routing_decision"]
    assert routed, f"vision_routing_decision not found in: {logs}"
    entry = routed[0]
    assert entry["trace_id"] == "test-trace"
    assert entry["session_id"] == "test-session"  # ctx.session_id, not trace_ctx.session_id
    assert entry["task_id"] is None
    assert entry["role_key"] == entry["effective_model_key"] == "primary"
    assert entry["escalated"] is False


@pytest.mark.asyncio
async def test_vision_routing_decision_log_absent_for_no_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No attachment on the turn — no vision routing decision to log."""
    import structlog

    from personal_agent.config import settings
    from personal_agent.telemetry.trace import TraceContext

    monkeypatch.setattr(settings, "skill_routing_mode", "hybrid")
    monkeypatch.setattr(settings, "skill_routing_model_key", "")

    ctx = _make_minimal_ctx_with_block_content()
    trace_ctx = TraceContext.new_trace()
    mock_llm = _make_mock_llm_client(_make_minimal_response())
    mock_session = MagicMock()
    mock_session.add_message = AsyncMock()
    mock_session.get_messages = AsyncMock(return_value=[])

    with (
        patch("personal_agent.orchestrator.skills.get_skill_block", return_value=""),
        patch("personal_agent.orchestrator.skills.assemble_skill_index", return_value=""),
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_index_directive",
            return_value="",
        ),
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_usage_directives",
            return_value="",
        ),
        patch("personal_agent.orchestrator.skills.get_all_skills", return_value={}),
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_llm),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        ),
        structlog.testing.capture_logs() as logs,
    ):
        from personal_agent.orchestrator.executor import step_llm_call

        await step_llm_call(ctx, mock_session, trace_ctx)  # type: ignore[arg-type]

    routed = [e for e in logs if e.get("event") == "vision_routing_decision"]
    assert routed == []


@pytest.mark.asyncio
async def test_vision_routing_decision_log_fires_for_document_forced_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-688 (ADR-0074 §8c, AC-11): a Tier-2 PDF that forces a routing decision.

    ``ctx.document_effective_model_key`` set at turn assembly (FRE-684) must
    log ``vision_routing_decision`` with the turn's identity — the
    routing-telemetry leg of document-path joinability.
    """
    import structlog

    from personal_agent.config import settings
    from personal_agent.orchestrator.types import AttachmentRef
    from personal_agent.telemetry.trace import TraceContext

    monkeypatch.setattr(settings, "skill_routing_mode", "hybrid")
    monkeypatch.setattr(settings, "skill_routing_model_key", "")

    ctx = _make_minimal_ctx_with_block_content()
    ctx.attachments = (
        AttachmentRef(
            artifact_id="doc-456",
            content_type="application/pdf",
            title="scan.pdf",
            r2_key="upload/user/GLOBAL/scan.pdf",
        ),
    )
    # Simulates step_init having classified this PDF Tier 2 and resolved a
    # forced routing decision (FRE-684) — the document-driven leg of the
    # condition at executor.py's vision_routing_decision log site.
    ctx.document_effective_model_key = "claude_sonnet"
    trace_ctx = TraceContext.new_trace()
    mock_llm = _make_mock_llm_client(_make_minimal_response())
    mock_session = MagicMock()
    mock_session.add_message = AsyncMock()
    mock_session.get_messages = AsyncMock(return_value=[])

    with (
        patch("personal_agent.orchestrator.skills.get_skill_block", return_value=""),
        patch("personal_agent.orchestrator.skills.assemble_skill_index", return_value=""),
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_index_directive",
            return_value="",
        ),
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_usage_directives",
            return_value="",
        ),
        patch("personal_agent.orchestrator.skills.get_all_skills", return_value={}),
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_llm),
        patch(
            "personal_agent.llm_client.factory.get_llm_client_for_key",
            return_value=mock_llm,
        ),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        ),
        structlog.testing.capture_logs() as logs,
    ):
        from personal_agent.orchestrator.executor import step_llm_call

        await step_llm_call(ctx, mock_session, trace_ctx)  # type: ignore[arg-type]

    routed = [e for e in logs if e.get("event") == "vision_routing_decision"]
    assert routed, f"vision_routing_decision not found in: {logs}"
    entry = routed[0]
    assert entry["trace_id"] == "test-trace"
    assert entry["session_id"] == "test-session"
    assert entry["task_id"] is None
    assert entry["effective_model_key"] == "claude_sonnet"
    assert entry["escalated"] is True


@pytest.mark.asyncio
async def test_vision_routing_decision_log_absent_for_tier1_pdf_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-684 regression: a Tier-1 (text) PDF never triggers a routing decision

    (ADR-0102 §1 — it works on any model), so no ``vision_routing_decision``
    should be logged for it — logging a no-op decision on every plain-text PDF
    turn would dilute the ADR-0074 §8c signal (code-review finding).
    """
    import structlog

    from personal_agent.config import settings
    from personal_agent.orchestrator.types import AttachmentRef
    from personal_agent.telemetry.trace import TraceContext

    monkeypatch.setattr(settings, "skill_routing_mode", "hybrid")
    monkeypatch.setattr(settings, "skill_routing_model_key", "")

    ctx = _make_minimal_ctx_with_block_content()
    ctx.attachments = (
        AttachmentRef(
            artifact_id="doc-123",
            content_type="application/pdf",
            title="report.pdf",
            r2_key="upload/user/GLOBAL/report.pdf",
        ),
    )
    # Simulates step_init having classified this PDF Tier 1 (text) — no
    # document-driven routing decision was ever made, so this stays None.
    ctx.document_effective_model_key = None
    trace_ctx = TraceContext.new_trace()
    mock_llm = _make_mock_llm_client(_make_minimal_response())
    mock_session = MagicMock()
    mock_session.add_message = AsyncMock()
    mock_session.get_messages = AsyncMock(return_value=[])

    with (
        patch("personal_agent.orchestrator.skills.get_skill_block", return_value=""),
        patch("personal_agent.orchestrator.skills.assemble_skill_index", return_value=""),
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_index_directive",
            return_value="",
        ),
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_usage_directives",
            return_value="",
        ),
        patch("personal_agent.orchestrator.skills.get_all_skills", return_value={}),
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_llm),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        ),
        structlog.testing.capture_logs() as logs,
    ):
        from personal_agent.orchestrator.executor import step_llm_call

        await step_llm_call(ctx, mock_session, trace_ctx)  # type: ignore[arg-type]

    routed = [e for e in logs if e.get("event") == "vision_routing_decision"]
    assert routed == []
