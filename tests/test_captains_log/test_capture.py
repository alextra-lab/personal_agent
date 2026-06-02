"""Tests for Captain's Log capture module (Phase 2.2 / 2.3)."""

import pathlib
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import orjson

from personal_agent.captains_log.capture import (
    TaskCapture,
    write_capture,
)


def test_user_id_coercion() -> None:
    """Regression: asyncpg UUID must be coerced to uuid.UUID so orjson can serialize it."""
    import orjson
    from uuid import UUID as StdUUID

    _UUID_STR = "550e8400-e29b-41d4-a716-446655440000"
    _base: dict = dict(
        trace_id="trace-coerce",
        session_id="session-coerce",
        timestamp=datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc),
        user_message="test",
        outcome="completed",
    )

    # string input → uuid.UUID
    c1 = TaskCapture(**_base, user_id=_UUID_STR)
    assert isinstance(c1.user_id, StdUUID)
    orjson.dumps(c1.model_dump())

    # asyncpg-style subclass (isinstance passes but type() is not uuid.UUID) → must coerce
    class _SubUUID(StdUUID):
        pass

    c2_sub = TaskCapture(**_base, user_id=_SubUUID(_UUID_STR))
    assert type(c2_sub.user_id) is StdUUID  # strict type, not subclass
    orjson.dumps(c2_sub.model_dump())  # would fail with asyncpg UUID before this fix

    # duck-typed __str__ only → uuid.UUID
    class _FakeUUID:
        def __str__(self) -> str:
            return _UUID_STR

    c2 = TaskCapture(**_base, user_id=_FakeUUID())
    assert isinstance(c2.user_id, StdUUID)
    orjson.dumps(c2.model_dump())


_BASE: dict = dict(
    trace_id="trace-tok",
    session_id="session-tok",
    timestamp=datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
    user_message="hi",
    outcome="completed",
    user_id=str(uuid4()),
)


class TestTokenFieldCanonicalization:
    """FRE-377: TaskCapture uses input_tokens/output_tokens; legacy aliases still load."""

    def test_canonical_keys_populate_fields(self) -> None:
        """Constructing with canonical input_tokens/output_tokens works."""
        c = TaskCapture(**_BASE, input_tokens=10, output_tokens=5)
        assert c.input_tokens == 10
        assert c.output_tokens == 5

    def test_legacy_prompt_tokens_alias(self) -> None:
        """Legacy prompt_tokens key populates canonical input_tokens."""
        c = TaskCapture(**_BASE, prompt_tokens=42)
        assert c.input_tokens == 42

    def test_legacy_completion_tokens_alias(self) -> None:
        """Legacy completion_tokens key populates canonical output_tokens."""
        c = TaskCapture(**_BASE, completion_tokens=17)
        assert c.output_tokens == 17

    def test_legacy_json_deserialization(self) -> None:
        """Legacy on-disk JSON with prompt_tokens/completion_tokens loads into canonical fields."""
        raw = {
            **_BASE,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
        c = TaskCapture(**raw)
        assert c.input_tokens == 100
        assert c.output_tokens == 50

    def test_model_dump_emits_canonical_keys_not_legacy(self) -> None:
        """model_dump() output contains input_tokens/output_tokens, not legacy names."""
        c = TaskCapture(**_BASE, input_tokens=8, output_tokens=3)
        dumped = c.model_dump()
        assert "input_tokens" in dumped
        assert "output_tokens" in dumped
        assert "prompt_tokens" not in dumped
        assert "completion_tokens" not in dumped

    def test_model_dump_json_serializable(self) -> None:
        """model_dump() result is JSON-serializable with canonical field names."""
        c = TaskCapture(**_BASE, input_tokens=8, output_tokens=3)
        payload = orjson.dumps(c.model_dump(mode="json")).decode()
        assert '"input_tokens"' in payload
        assert '"output_tokens"' in payload
        assert '"prompt_tokens"' not in payload
        assert '"completion_tokens"' not in payload

    def test_defaults_are_zero(self) -> None:
        """Token fields default to 0 when not supplied."""
        c = TaskCapture(**_BASE)
        assert c.input_tokens == 0
        assert c.output_tokens == 0


class TestWriteCapture:
    """Test write_capture and optional ES indexing."""

    def test_write_capture_creates_file_and_indexes_to_es(self, tmp_path: pathlib.Path) -> None:
        """write_capture writes JSON to disk and calls schedule_es_index (Phase 2.3)."""
        capture = TaskCapture(
            trace_id="trace-123",
            session_id="session-456",
            timestamp=datetime(2026, 2, 22, 14, 0, 0, tzinfo=timezone.utc),
            user_message="Hello",
            assistant_response="Hi",
            outcome="completed",
            user_id=uuid4(),
        )
        with (
            patch(
                "personal_agent.captains_log.capture._get_captures_dir",
                return_value=tmp_path / "captures",
            ),
            patch("personal_agent.captains_log.capture.schedule_es_index") as mock_schedule,
        ):
            path = write_capture(capture)
            assert path.exists()
            assert path.suffix == ".json"
            mock_schedule.assert_called_once()
            from personal_agent.captains_log.capture import CAPTURES_INDEX_PREFIX  # noqa: PLC0415

            call_args = mock_schedule.call_args[0]
            assert call_args[0] == f"{CAPTURES_INDEX_PREFIX}-2026-02-22"
            assert call_args[1]["trace_id"] == "trace-123"
            assert call_args[1]["outcome"] == "completed"
            assert mock_schedule.call_args[1].get("doc_id") == "trace-123"
