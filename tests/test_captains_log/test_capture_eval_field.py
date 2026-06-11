"""FRE-523: eval provenance on capture models.

``TaskCapture`` and ``SubAgentCapture`` carry an ``eval_mode`` flag so
eval-derived capture/KG content is identifiable. Legacy on-disk capture files
predate the field and must still load (default ``False``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import orjson

from personal_agent.captains_log.capture import (
    SubAgentCapture,
    TaskCapture,
    read_captures,
)

_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


def _task_capture(**overrides: object) -> TaskCapture:
    base: dict[str, object] = {
        "trace_id": "trace-1",
        "session_id": "session-1",
        "timestamp": datetime.now(timezone.utc),
        "user_message": "hello",
        "outcome": "completed",
        "user_id": _USER_ID,
    }
    base.update(overrides)
    return TaskCapture(**base)  # type: ignore[arg-type]


def test_task_capture_eval_mode_defaults_false() -> None:
    """eval_mode defaults to False and survives a JSON round-trip."""
    capture = _task_capture()
    assert capture.eval_mode is False
    dumped = capture.model_dump(mode="json")
    assert dumped["eval_mode"] is False


def test_task_capture_eval_mode_true_roundtrip() -> None:
    """eval_mode=True is preserved through serialization."""
    capture = _task_capture(eval_mode=True)
    assert capture.eval_mode is True
    assert capture.model_dump(mode="json")["eval_mode"] is True


def test_sub_agent_capture_has_eval_mode() -> None:
    """SubAgentCapture carries eval_mode (default False)."""
    capture = SubAgentCapture(
        trace_id="trace-1",
        session_id="session-1",
        task_id="sub-abc",
        timestamp=datetime.now(timezone.utc),
        spec_task="do a thing",
        mode="single",
        model_role="sub_agent",
        max_tokens=512,
        full_output="out",
        full_output_chars=3,
        injected_digest="out",
        digest_chars=3,
        truncation_ratio=1.0,
        success=True,
        duration_ms=12.0,
        system_prompt_chars=0,
        skill_index_block_chars=0,
        context_message_count=0,
        context_chars=0,
        eval_mode=True,
    )
    assert capture.eval_mode is True
    assert SubAgentCapture.model_fields["eval_mode"].default is False


def test_read_captures_tolerates_legacy_file_without_eval_mode(tmp_path, monkeypatch) -> None:
    """Pre-FRE-523 capture files (no eval_mode key) load with eval_mode=False."""
    captures_root = tmp_path / "captures"
    date_dir = captures_root / "2026-06-11"
    date_dir.mkdir(parents=True)
    legacy = {
        "trace_id": "legacy-trace",
        "session_id": "legacy-session",
        "timestamp": "2026-06-11T00:00:00+00:00",
        "user_message": "legacy",
        "outcome": "completed",
        "user_id": str(_USER_ID),
        # note: no "eval_mode" key
    }
    (date_dir / "legacy-trace.json").write_bytes(orjson.dumps(legacy))

    monkeypatch.setattr(
        "personal_agent.captains_log.capture._get_captures_dir", lambda: captures_root
    )
    captures = read_captures()
    assert len(captures) == 1
    assert captures[0].eval_mode is False
