"""Tests for cloud-attachment cost confirmation and re-injection (FRE-749 / ADR-0101 §8b)."""

import time

import pytest

from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.types import (
    AttachmentRef,
    ExecutionContext,
    PendingCloudAttachmentConfirmation,
)


class TestPendingCloudConfirmationPersistence:
    """Tests for SessionManager pending confirmation methods."""

    def test_save_and_load_pending_confirmation(self):
        """Pending confirmation is saved and loaded from session metadata."""
        mgr = SessionManager()
        session_id = mgr.create_session(Mode.NORMAL, Channel.CHAT)

        pending = PendingCloudAttachmentConfirmation(
            attachments=(
                AttachmentRef(
                    artifact_id="art-1",
                    content_type="image/jpeg",
                    title="test.jpg",
                    r2_key="uploads/test.jpg",
                ),
            ),
            cloud_vision_model_key="claude-model-vision",
            estimate_usd=0.0048,
            created_at=time.time(),
            ttl_seconds=600,
            original_trace_id="trace-123",
        )

        mgr.save_pending_confirmation(session_id, pending)
        loaded = mgr.load_pending_confirmation(session_id)

        assert loaded is not None
        assert loaded["cloud_vision_model_key"] == "claude-model-vision"
        assert loaded["estimate_usd"] == 0.0048
        assert len(loaded["attachments"]) == 1
        assert loaded["attachments"][0]["artifact_id"] == "art-1"

    def test_pending_confirmation_expires(self):
        """Pending confirmation returns None when TTL expires."""
        mgr = SessionManager()
        session_id = mgr.create_session(Mode.NORMAL, Channel.CHAT)

        pending = PendingCloudAttachmentConfirmation(
            attachments=(),
            cloud_vision_model_key="claude-model-vision",
            estimate_usd=0.0048,
            created_at=time.time() - 1000,  # 1000 seconds ago
            ttl_seconds=60,  # 60 second TTL
            original_trace_id="trace-123",
        )

        mgr.save_pending_confirmation(session_id, pending)
        loaded = mgr.load_pending_confirmation(session_id)

        assert loaded is None

    def test_clear_pending_confirmation(self):
        """Pending confirmation is cleared from session metadata."""
        mgr = SessionManager()
        session_id = mgr.create_session(Mode.NORMAL, Channel.CHAT)

        pending = PendingCloudAttachmentConfirmation(
            attachments=(),
            cloud_vision_model_key="claude-model-vision",
            estimate_usd=0.0048,
            created_at=time.time(),
            ttl_seconds=600,
            original_trace_id="trace-123",
        )

        mgr.save_pending_confirmation(session_id, pending)
        assert mgr.load_pending_confirmation(session_id) is not None

        mgr.clear_pending_confirmation(session_id)
        assert mgr.load_pending_confirmation(session_id) is None

    def test_pending_confirmation_not_found(self):
        """load_pending_confirmation returns None when no confirmation exists."""
        mgr = SessionManager()
        session_id = mgr.create_session(Mode.NORMAL, Channel.CHAT)

        loaded = mgr.load_pending_confirmation(session_id)
        assert loaded is None

    def test_nonexistent_session_raises(self):
        """save_pending_confirmation raises ValueError for nonexistent session."""
        mgr = SessionManager()
        pending = PendingCloudAttachmentConfirmation(
            attachments=(),
            cloud_vision_model_key="claude-model-vision",
            estimate_usd=0.0048,
            created_at=time.time(),
            ttl_seconds=600,
            original_trace_id="trace-123",
        )

        with pytest.raises(ValueError, match="Session .* not found"):
            mgr.save_pending_confirmation("nonexistent-session", pending)


class TestAffirmativeConfirmationDetection:
    """Tests for _is_affirmative_confirmation helper."""

    @pytest.mark.asyncio
    async def test_affirmative_messages_detected(self):
        """Common affirmative phrases are correctly detected."""
        from personal_agent.orchestrator.executor import _is_affirmative_confirmation

        affirmative_messages = [
            "proceed",
            "yes",
            "ok",
            "okay",
            "confirm",
            "cloud",
            "Proceed on cloud",
            "Yes, proceed",
            "proceed on cloud please",
            "PROCEED",
            "  yes  ",  # with whitespace
        ]

        for msg in affirmative_messages:
            assert _is_affirmative_confirmation(msg), f"Should detect as affirmative: {msg}"

    @pytest.mark.asyncio
    async def test_non_affirmative_messages_rejected(self):
        """Non-affirmative or ambiguous messages are not detected."""
        from personal_agent.orchestrator.executor import _is_affirmative_confirmation

        non_affirmative = [
            "keep it local",
            "no cloud",
            "Use the local model",
            "What does the image show?",
            "Yes, I agree with that",  # "yes" but not about confirmation
            "Is that a yes or no?",  # contains "yes" but not affirmative
            "",
            "   ",
        ]

        for msg in non_affirmative:
            assert not _is_affirmative_confirmation(msg), f"Should not detect as affirmative: {msg}"


class TestPendingAttachmentReinjection:
    """Tests for _maybe_reinject_pending_cloud_attachment."""

    @pytest.mark.asyncio
    async def test_reinject_on_affirmative_message(self):
        """Pending attachments are re-injected when user message is affirmative."""
        from personal_agent.orchestrator.executor import _maybe_reinject_pending_cloud_attachment

        mgr = SessionManager()
        session_id = mgr.create_session(Mode.NORMAL, Channel.CHAT)

        # Create and save pending confirmation
        original_attachment = AttachmentRef(
            artifact_id="art-1",
            content_type="image/jpeg",
            title="test.jpg",
            r2_key="uploads/test.jpg",
        )
        pending = PendingCloudAttachmentConfirmation(
            attachments=(original_attachment,),
            cloud_vision_model_key="claude-model-vision",
            estimate_usd=0.0048,
            created_at=time.time(),
            ttl_seconds=600,
            original_trace_id="trace-123",
        )
        mgr.save_pending_confirmation(session_id, pending)

        # Create context with affirmative message but no attachments
        ctx = ExecutionContext(
            session_id=session_id,
            trace_id="trace-456",
            user_message="Proceed",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            attachments=(),  # Empty initially
        )

        # Re-inject
        await _maybe_reinject_pending_cloud_attachment(ctx, mgr)

        # Verify attachments were re-injected
        assert len(ctx.attachments) == 1
        assert ctx.attachments[0].artifact_id == "art-1"
        assert ctx.attachments[0].title == "test.jpg"

        # Verify pending confirmation was cleared
        assert mgr.load_pending_confirmation(session_id) is None

    @pytest.mark.asyncio
    async def test_no_reinject_on_non_affirmative_message(self):
        """Pending attachments are not re-injected when user message is non-affirmative."""
        from personal_agent.orchestrator.executor import _maybe_reinject_pending_cloud_attachment

        mgr = SessionManager()
        session_id = mgr.create_session(Mode.NORMAL, Channel.CHAT)

        # Create and save pending confirmation
        pending = PendingCloudAttachmentConfirmation(
            attachments=(
                AttachmentRef(
                    artifact_id="art-1",
                    content_type="image/jpeg",
                    title="test.jpg",
                    r2_key="uploads/test.jpg",
                ),
            ),
            cloud_vision_model_key="claude-model-vision",
            estimate_usd=0.0048,
            created_at=time.time(),
            ttl_seconds=600,
            original_trace_id="trace-123",
        )
        mgr.save_pending_confirmation(session_id, pending)

        # Create context with non-affirmative message
        ctx = ExecutionContext(
            session_id=session_id,
            trace_id="trace-456",
            user_message="Keep it local",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            attachments=(),
        )

        # Re-inject
        await _maybe_reinject_pending_cloud_attachment(ctx, mgr)

        # Verify attachments were NOT re-injected
        assert len(ctx.attachments) == 0

        # Verify pending confirmation was cleared
        assert mgr.load_pending_confirmation(session_id) is None

    @pytest.mark.asyncio
    async def test_no_pending_state_leaves_context_unchanged(self):
        """Re-injection with no pending state leaves context unchanged."""
        from personal_agent.orchestrator.executor import _maybe_reinject_pending_cloud_attachment

        mgr = SessionManager()
        session_id = mgr.create_session(Mode.NORMAL, Channel.CHAT)

        ctx = ExecutionContext(
            session_id=session_id,
            trace_id="trace-456",
            user_message="Proceed",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            attachments=(),
        )

        await _maybe_reinject_pending_cloud_attachment(ctx, mgr)

        # Verify no change
        assert len(ctx.attachments) == 0
