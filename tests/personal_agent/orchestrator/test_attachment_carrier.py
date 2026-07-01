"""Tests for the structured attachment carrier (FRE-661 / ADR-0101 §2, §8a).

Proves AC-5 (clean task description) and the AC-9 slice (processing_target
threaded end to end) at the handle_user_request -> ExecutionContext seam.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.orchestrator import Orchestrator
from personal_agent.orchestrator.types import AttachmentRef, ExecutionContext


def _make_attachment(**overrides: object) -> AttachmentRef:
    defaults: dict[str, object] = {
        "artifact_id": "abc-123",
        "content_type": "image/png",
        "title": "photo.png",
        "r2_key": "upload/user/GLOBAL/abc.png",
    }
    defaults.update(overrides)
    return AttachmentRef(**defaults)  # type: ignore[arg-type]


class TestAttachmentRef:
    def test_is_frozen(self) -> None:
        ref = _make_attachment()
        with pytest.raises(FrozenInstanceError):
            ref.title = "other.png"  # type: ignore[misc]

    def test_processing_target_defaults_none(self) -> None:
        ref = _make_attachment()
        assert ref.processing_target is None

    def test_processing_target_accepts_cloud_or_local(self) -> None:
        assert _make_attachment(processing_target="cloud").processing_target == "cloud"
        assert _make_attachment(processing_target="local").processing_target == "local"


class TestExecutionContextAttachmentsField:
    def test_defaults_to_empty_tuple(self) -> None:
        ctx = ExecutionContext(
            session_id="s1",
            trace_id="t1",
            user_message="hello",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )
        assert ctx.attachments == ()


class TestHandleUserRequestAttachmentCarrier:
    @pytest.mark.asyncio
    async def test_user_message_stays_byte_for_byte_clean_ac5(self) -> None:
        """AC-5: ctx.user_message equals the original text; no attachment metadata in it."""
        original_message = "Please review this."
        attachment = _make_attachment()
        orchestrator = Orchestrator()

        captured: dict[str, ExecutionContext] = {}

        async def _fake_execute_task_safe(ctx: ExecutionContext, session_manager: object) -> object:
            captured["ctx"] = ctx
            return {"reply": "ok", "steps": [], "trace_id": ctx.trace_id}

        with patch(
            "personal_agent.orchestrator.orchestrator.execute_task_safe",
            new=AsyncMock(side_effect=_fake_execute_task_safe),
        ):
            await orchestrator.handle_user_request(
                session_id=str(uuid4()),
                user_message=original_message,
                attachments=[attachment],
            )

        ctx = captured["ctx"]
        assert ctx.user_message == original_message
        assert attachment.artifact_id not in ctx.user_message
        assert attachment.content_type not in ctx.user_message
        assert attachment.title not in ctx.user_message
        assert attachment.r2_key not in ctx.user_message

    @pytest.mark.asyncio
    async def test_attachments_carried_separately_on_ctx(self) -> None:
        attachment = _make_attachment()
        orchestrator = Orchestrator()
        captured: dict[str, ExecutionContext] = {}

        async def _fake_execute_task_safe(ctx: ExecutionContext, session_manager: object) -> object:
            captured["ctx"] = ctx
            return {"reply": "ok", "steps": [], "trace_id": ctx.trace_id}

        with patch(
            "personal_agent.orchestrator.orchestrator.execute_task_safe",
            new=AsyncMock(side_effect=_fake_execute_task_safe),
        ):
            await orchestrator.handle_user_request(
                session_id=str(uuid4()),
                user_message="hi",
                attachments=[attachment],
            )

        ctx = captured["ctx"]
        assert ctx.attachments == (attachment,)

    @pytest.mark.asyncio
    async def test_processing_target_threaded_unchanged_ac9(self) -> None:
        """AC-9 slice: processing_target set on the inbound attachment reaches ctx unchanged."""
        attachment = _make_attachment(processing_target="cloud")
        orchestrator = Orchestrator()
        captured: dict[str, ExecutionContext] = {}

        async def _fake_execute_task_safe(ctx: ExecutionContext, session_manager: object) -> object:
            captured["ctx"] = ctx
            return {"reply": "ok", "steps": [], "trace_id": ctx.trace_id}

        with patch(
            "personal_agent.orchestrator.orchestrator.execute_task_safe",
            new=AsyncMock(side_effect=_fake_execute_task_safe),
        ):
            await orchestrator.handle_user_request(
                session_id=str(uuid4()),
                user_message="hi",
                attachments=[attachment],
            )

        ctx = captured["ctx"]
        assert ctx.attachments[0].processing_target == "cloud"

    @pytest.mark.asyncio
    async def test_no_attachments_defaults_to_empty_tuple(self) -> None:
        orchestrator = Orchestrator()
        captured: dict[str, ExecutionContext] = {}

        async def _fake_execute_task_safe(ctx: ExecutionContext, session_manager: object) -> object:
            captured["ctx"] = ctx
            return {"reply": "ok", "steps": [], "trace_id": ctx.trace_id}

        with patch(
            "personal_agent.orchestrator.orchestrator.execute_task_safe",
            new=AsyncMock(side_effect=_fake_execute_task_safe),
        ):
            await orchestrator.handle_user_request(
                session_id=str(uuid4()),
                user_message="hi",
            )

        ctx = captured["ctx"]
        assert ctx.attachments == ()
