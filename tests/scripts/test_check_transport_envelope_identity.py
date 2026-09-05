# ruff: noqa: D103
"""Unit tests for the transport-envelope identity lint (FRE-427).

Extends the ADR-0074 joinability discipline to client-facing transport
envelopes. Flags:
  - a ``{"type": "DONE", ...}`` dict literal missing ``trace_id``
  - an ``emit_turn_status(..., value={...})`` call whose literal lacks ``trace_id``
  - an adapter ``case <EventClass>(...):`` arm that drops ``trace_id`` for an
    event dataclass that declares it
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from scripts.check_transport_envelope_identity import (
    _lint_tree,
    lint_adapter_completeness,
    lint_file,
)


def test_done_literal_without_trace_id_is_flagged(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def emit_done(session_id: str) -> None:
                payload = {"type": "DONE"}
            """
        )
    )
    violations = lint_file(src)
    assert [v.kind for v in violations] == ["done_missing_trace_id"]


def test_done_literal_with_trace_id_is_clean(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def emit_done(session_id: str, trace_id: str) -> None:
                payload = {"type": "DONE", "trace_id": trace_id}
            """
        )
    )
    assert lint_file(src) == []


def test_unrelated_done_string_is_not_a_dict_and_is_ignored(tmp_path: Path) -> None:
    """A bare string "DONE" (e.g. an event_type argument) is not an envelope dict."""
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def f(buf):
                await buf.append(event_type="DONE", payload={"type": "DONE", "trace_id": "t"})
            """
        )
    )
    assert lint_file(src) == []


def test_turn_status_call_with_literal_missing_trace_id_is_flagged(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def f(session_id):
                await emit_turn_status(session_id=session_id, value={"context_tokens": 10})
            """
        )
    )
    violations = lint_file(src)
    assert [v.kind for v in violations] == ["turn_status_missing_trace_id"]


def test_turn_status_call_with_literal_trace_id_is_clean(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def f(session_id, trace_id):
                await emit_turn_status(
                    session_id=session_id, value={"context_tokens": 10, "trace_id": trace_id}
                )
            """
        )
    )
    assert lint_file(src) == []


def test_turn_status_call_with_opaque_value_is_trusted(tmp_path: Path) -> None:
    """A non-literal ``value=`` (e.g. ``dict(value)`` from a typed source) is exempt."""
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def f(session_id, value):
                await emit_turn_status(session_id=session_id, value=dict(value))
            """
        )
    )
    assert lint_file(src) == []


def test_trace_allow_suppresses_a_marked_violation(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def emit_done(session_id: str) -> None:
                payload = {"type": "DONE"}  # trace-allow: legacy test fixture, tracked in FRE-999
            """
        )
    )
    assert lint_file(src) == []
    assert lint_file(src, strict=True) != []


# ── Adapter completeness (cross-file) ───────────────────────────────────────


_EVENTS_SRC = textwrap.dedent(
    """
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class CancelledEvent:
        session_id: str
        trace_id: str
        reason: str

    @dataclass(frozen=True)
    class TextDeltaEvent:
        text: str
        session_id: str
    """
)


def test_adapter_drops_trace_id_for_a_trace_bearing_event(tmp_path: Path) -> None:
    events = tmp_path / "events.py"
    events.write_text(_EVENTS_SRC)
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        textwrap.dedent(
            """
            from events import CancelledEvent, TextDeltaEvent

            def to_agui_event(event):
                match event:
                    case TextDeltaEvent(text=text, session_id=sid):
                        envelope = {"type": "TEXT_DELTA", "data": {"text": text}, "session_id": sid}
                    case CancelledEvent(session_id=sid, reason=reason):
                        envelope = {"type": "CANCELLED", "session_id": sid, "data": {"reason": reason}}
                return envelope
            """
        )
    )
    violations = lint_adapter_completeness(events, adapter)
    assert [(v.kind, v.detail) for v in violations] == [
        ("adapter_drops_trace_id", "CancelledEvent")
    ]


def test_adapter_forwarding_trace_id_is_clean(tmp_path: Path) -> None:
    events = tmp_path / "events.py"
    events.write_text(_EVENTS_SRC)
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        textwrap.dedent(
            """
            from events import CancelledEvent, TextDeltaEvent

            def to_agui_event(event):
                match event:
                    case TextDeltaEvent(text=text, session_id=sid):
                        envelope = {"type": "TEXT_DELTA", "data": {"text": text}, "session_id": sid}
                    case CancelledEvent(session_id=sid, trace_id=trace_id, reason=reason):
                        envelope = {
                            "type": "CANCELLED",
                            "session_id": sid,
                            "trace_id": trace_id,
                            "data": {"reason": reason},
                        }
                return envelope
            """
        )
    )
    assert lint_adapter_completeness(events, adapter) == []


def test_adapter_trace_allow_suppresses_a_marked_case(tmp_path: Path) -> None:
    events = tmp_path / "events.py"
    events.write_text(_EVENTS_SRC)
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        textwrap.dedent(
            """
            from events import CancelledEvent

            def to_agui_event(event):
                match event:
                    case CancelledEvent(session_id=sid, reason=reason):  # trace-allow: FRE-999 known gap
                        envelope = {"type": "CANCELLED", "session_id": sid, "data": {"reason": reason}}
                return envelope
            """
        )
    )
    assert lint_adapter_completeness(events, adapter) == []
    assert lint_adapter_completeness(events, adapter, strict=True) != []


# ── Tree discovery must resolve the real events.py / adapter.py, not just any
# file with that basename (the repo also has telemetry/events.py) ──────────


def test_lint_tree_resolves_events_by_path_not_bare_filename(tmp_path: Path) -> None:
    """A same-named decoy ``events.py`` elsewhere in the tree must not shadow
    ``transport/events.py`` — regression guard for a bug where ``_lint_tree``
    matched on bare filename, picked whichever ``events.py`` sorted first
    (alphabetically, ``telemetry`` < ``transport``), and silently emptied the
    trace_id-bearing class set, defusing ``adapter_drops_trace_id`` entirely.
    """
    # Decoy: a same-named events.py with no dataclasses, sorting before the
    # real one (mirrors telemetry/events.py preceding transport/events.py).
    decoy_dir = tmp_path / "telemetry"
    decoy_dir.mkdir()
    (decoy_dir / "events.py").write_text("TASK_OUTCOME_COMPLETED = 'completed'\n")

    transport_dir = tmp_path / "transport"
    transport_dir.mkdir()
    (transport_dir / "events.py").write_text(_EVENTS_SRC)

    agui_dir = transport_dir / "agui"
    agui_dir.mkdir()
    (agui_dir / "adapter.py").write_text(
        textwrap.dedent(
            """
            from events import CancelledEvent, TextDeltaEvent

            def to_agui_event(event):
                match event:
                    case TextDeltaEvent(text=text, session_id=sid):
                        envelope = {"type": "TEXT_DELTA", "data": {"text": text}, "session_id": sid}
                    case CancelledEvent(session_id=sid, reason=reason):
                        envelope = {"type": "CANCELLED", "session_id": sid, "data": {"reason": reason}}
                return envelope
            """
        )
    )

    violations = _lint_tree(tmp_path, strict=False)
    assert [(v.kind, v.detail) for v in violations] == [
        ("adapter_drops_trace_id", "CancelledEvent")
    ]
