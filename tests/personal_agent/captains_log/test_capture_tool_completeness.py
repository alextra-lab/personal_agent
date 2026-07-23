"""Capture completeness for ADR-0124 AC-8 (FRE-947).

AC-8 requires the summariser's assembled prompt to carry, for **every tool
invocation**, its name, **arguments**, status and error. Two gaps stood in the way,
neither recoverable after the fact:

* the dispatched path recorded no ``arguments`` — they survived only in the
  intra-turn digest sidecar and were dropped before the capture was written;
* three paths abandoned an invocation before dispatch (malformed argument JSON,
  loop-gate block, escaped dispatch exception) and appended **only** to the
  transcript, so the capture never saw them at all.

These tests pin both, plus the one behavioural consequence: the degraded-path
fallback reply now mentions the failed invocations it previously stayed silent
about.
"""

# ruff: noqa: D103

from __future__ import annotations

import json

from personal_agent.captains_log.es_indexer import normalize_capture_doc_for_es
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.executor import (
    _fallback_reply_from_tool_results,
    _record_undispatched_invocation,
)
from personal_agent.orchestrator.types import ExecutionContext


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        session_id="sess-1",
        trace_id="t-1",
        user_message="hi",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )


def test_undispatched_invocation_is_recorded_with_status_and_error() -> None:
    ctx = _ctx()
    _record_undispatched_invocation(
        ctx,
        tool_name="read_file",
        arguments={"path": "/etc/hosts"},
        error="blocked by loop gate: block_consecutive",
    )

    assert len(ctx.tool_results) == 1
    entry = ctx.tool_results[0]
    assert entry["tool_name"] == "read_file"
    assert entry["success"] is False
    assert entry["error"] == "blocked by loop gate: block_consecutive"
    assert entry["arguments"] == {"path": "/etc/hosts"}


def test_malformed_arguments_are_recorded_as_the_raw_string() -> None:
    """Parsing is what failed, so there is no dict to record — keep the raw text.

    Discarding it would leave the summariser unable to say what was attempted.
    """
    ctx = _ctx()
    _record_undispatched_invocation(
        ctx,
        tool_name="query_elasticsearch",
        arguments='{"index": "agent-logs-*"',  # truncated JSON
        error="malformed argument JSON: Expecting ',' delimiter",
    )

    assert ctx.tool_results[0]["arguments"] == '{"index": "agent-logs-*"'
    assert ctx.tool_results[0]["success"] is False


def test_fallback_reply_surfaces_undispatched_invocations() -> None:
    """The one behavioural consumer of ctx.tool_results — pinned deliberately.

    Before FRE-947 a turn whose only tool calls were gate-blocked produced the
    "I couldn't produce a final answer" branch, because ctx.tool_results was empty.
    It now renders the blocked call under the existing `failed (<error>)` contract,
    which is strictly more informative and not a new format.
    """
    ctx = _ctx()
    _record_undispatched_invocation(
        ctx,
        tool_name="web_search",
        arguments={"query": "neo4j sharding"},
        error="blocked by loop gate: block_identity",
    )

    reply = _fallback_reply_from_tool_results(ctx)

    assert "web_search: failed (blocked by loop gate: block_identity)" in reply
    assert "I couldn't produce a final answer" not in reply


def test_es_normaliser_stringifies_arguments() -> None:
    """F1b — an arguments dict must never reach ES as an object.

    The captures mapping declares `dynamic: true` at its root, so an object of
    arbitrary tool-specific keys would be mapped one field per key.
    """
    doc = {
        "tool_results": [
            {
                "tool_name": "read_file",
                "success": True,
                "output": {"lines": 3},
                "error": None,
                "arguments": {"path": "/etc/hosts", "encoding": "utf-8"},
            }
        ]
    }

    normalised = normalize_capture_doc_for_es(doc)

    entry = normalised["tool_results"][0]
    assert isinstance(entry["arguments"], str)
    assert isinstance(entry["output"], str)
    assert json.loads(entry["arguments"]) == {"path": "/etc/hosts", "encoding": "utf-8"}
    # Input is not mutated.
    assert isinstance(doc["tool_results"][0]["arguments"], dict)


def test_es_normaliser_leaves_a_string_argument_alone() -> None:
    """The malformed-JSON path already stores a string; re-encoding would double-quote it."""
    doc = {
        "tool_results": [
            {"tool_name": "x", "success": False, "output": None, "arguments": '{"broken": '}
        ]
    }

    normalised = normalize_capture_doc_for_es(doc)

    assert normalised["tool_results"][0]["arguments"] == '{"broken": '


def test_es_normaliser_does_not_invent_an_arguments_field() -> None:
    """Historical entries carry no arguments; the normaliser must not add a null one."""
    doc = {"tool_results": [{"tool_name": "x", "success": True, "output": "ok"}]}

    normalised = normalize_capture_doc_for_es(doc)

    assert "arguments" not in normalised["tool_results"][0]


def test_session_filter_is_applied_inside_the_scan(tmp_path, monkeypatch) -> None:
    """M-3 — `limit` must bound MATCHING captures, not captures examined.

    read_captures walks date directories newest-first. Filtering by session after
    the scan let a busy window's other sessions consume the whole budget, silently
    dropping the target session's EARLIEST turns — while the producer went on to
    assert "Full transcript follows. Nothing has been truncated." That is exactly
    the silent truncation ADR-0124 forbids, and it fabricates contradictions.
    """
    import json as _json
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    from personal_agent.captains_log import capture as cap

    day = tmp_path / "2026-07-20"
    day.mkdir(parents=True)
    base = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)

    def _write(trace_id: str, session_id: str, minute: int) -> None:
        (day / f"{trace_id}.json").write_text(
            _json.dumps(
                {
                    "trace_id": trace_id,
                    "session_id": session_id,
                    "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                    "user_message": f"m{minute}",
                    "assistant_response": "ok",
                    "outcome": "completed",
                    "user_id": str(uuid4()),
                    "tool_results": [],
                }
            )
        )

    # The target session's two turns, plus plenty of noise from other sessions.
    _write("target-a", "target", 1)
    _write("target-b", "target", 9)
    for i in range(20):
        _write(f"noise-{i:02d}", f"other-{i}", 2 + i % 5)

    monkeypatch.setattr(cap, "_get_captures_dir", lambda: tmp_path)

    got = cap.read_session_captures(
        "target", started_at=base, ended_at=base + timedelta(minutes=9), limit=5
    )

    assert [c.trace_id for c in got] == ["target-a", "target-b"], (
        "both turns must survive a limit that the noise sessions would otherwise consume"
    )
