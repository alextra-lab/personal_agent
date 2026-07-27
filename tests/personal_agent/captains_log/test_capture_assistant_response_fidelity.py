"""AC-5 tail (ADR-0125 D5): assistant-response fidelity through the storage seam.

D3 item 2 ("assistant response, full text") is asserted by the ADR to already
exist today, unlike items 5/6. This is the confirmation the ticket's AC-5 check
asks for: run a response that materially exceeds 200 characters and confirm the
stored byte length equals the emitted byte length — through the *real*
persistence functions (disk write/read, ES normalization), not a bare
construction check that a codex plan review flagged as only proving
serialization after the value already crossed the seam.

``TaskCapture.assistant_response`` carries no length constraint (verified by
reading ``captains_log/capture.py``'s field definition), and the executor
assigns it directly from ``ctx.final_reply`` with no intervening slice —
confirmed independently by ``scripts/check_evidence_truncation.py`` finding
zero violations at that assignment site.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

from personal_agent.captains_log.capture import TaskCapture, read_captures, write_capture
from personal_agent.captains_log.es_indexer import normalize_capture_doc_for_es

# p99 assistant_response length re-derived 2026-07-27 against real captures
# (agent-captains-captures-*, N=1864) is 7940 chars; this exceeds even that,
# materially clearing the 200-char threshold AC-5 names.
_LONG_RESPONSE = "The answer involves several considerations. " * 200


def _capture(trace_id: str, **overrides: object) -> TaskCapture:
    defaults: dict[str, object] = {
        "trace_id": trace_id,
        "session_id": "sess-fidelity",
        "timestamp": datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc),
        "user_message": "explain the tradeoffs",
        "assistant_response": _LONG_RESPONSE,
        "outcome": "completed",
        "user_id": uuid4(),
    }
    defaults.update(overrides)
    return TaskCapture(**defaults)  # type: ignore[arg-type]


def test_long_assistant_response_exceeds_the_retired_200_char_idiom() -> None:
    assert len(_LONG_RESPONSE.encode("utf-8")) > 200


def test_disk_round_trip_preserves_assistant_response_byte_length(
    tmp_path: pathlib.Path,
) -> None:
    capture = _capture("trace-fidelity-disk")
    emitted_bytes = len(capture.assistant_response.encode("utf-8"))  # type: ignore[union-attr]

    with (
        patch(
            "personal_agent.captains_log.capture._get_captures_dir",
            return_value=tmp_path / "captures",
        ),
        patch("personal_agent.captains_log.capture.schedule_es_index"),
    ):
        write_capture(capture)
        read_back = read_captures(
            start_date=datetime(2026, 7, 27, tzinfo=timezone.utc),
            end_date=datetime(2026, 7, 27, 23, 59, tzinfo=timezone.utc),
            session_id="sess-fidelity",
        )

    assert len(read_back) == 1
    stored_bytes = len(read_back[0].assistant_response.encode("utf-8"))
    assert stored_bytes == emitted_bytes


def test_es_normalize_does_not_touch_assistant_response() -> None:
    capture = _capture("trace-fidelity-es")
    doc = capture.model_dump(mode="json")
    emitted_bytes = len(capture.assistant_response.encode("utf-8"))  # type: ignore[union-attr]

    normalized = normalize_capture_doc_for_es(doc)

    assert len(normalized["assistant_response"].encode("utf-8")) == emitted_bytes
    assert normalized["assistant_response"] == capture.assistant_response
