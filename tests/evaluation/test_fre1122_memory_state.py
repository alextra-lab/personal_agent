"""FRE-1122's report gains a memory-state column, read from the evidence record (AC-7,
ADR-0148). ``test_fre1122_run_phase.py`` covers the run phase's identity/session/timeout
concerns; this covers the capture-reading helpers the report's new column reads from.

The bar AC-7 states directly: the column must be populated from the turn-evidence
record, never inferred from ``rendered_memory``'s item count — a ``NOTHING_RELEVANT``
turn and an ``UNAVAILABLE`` one both render an empty item list.
"""

from __future__ import annotations

import json
import pathlib

from scripts.eval.fre1122_absence_probe.runner import (
    _CAPTURE_MISSING,
    _MEMORY_STATE_UNKNOWN,
    _load_memory_state,
    _load_rendered_memory,
    _read_capture,
)


def _write_capture(root: pathlib.Path, trace_id: str, recall_admission: dict) -> None:
    day_dir = root / "2026-09-11"
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / f"{trace_id}.json").write_text(json.dumps({"recall_admission": recall_admission}))


class TestReadCapture:
    def test_missing_capture_returns_none(self, tmp_path: pathlib.Path) -> None:
        assert _read_capture(tmp_path, "no-such-trace") is None

    def test_unreadable_capture_returns_none(self, tmp_path: pathlib.Path) -> None:
        day_dir = tmp_path / "2026-09-11"
        day_dir.mkdir()
        (day_dir / "bad-trace.json").write_text("{not json")

        assert _read_capture(tmp_path, "bad-trace") is None

    def test_a_written_capture_is_read_back(self, tmp_path: pathlib.Path) -> None:
        _write_capture(tmp_path, "t-1", {"memory_state": "populated"})

        capture = _read_capture(tmp_path, "t-1")

        assert capture is not None
        assert capture["recall_admission"]["memory_state"] == "populated"


class TestLoadMemoryState:
    """AC-7: the state per probe, read from the evidence record."""

    def test_missing_capture_is_unknown_not_inferred(self, tmp_path: pathlib.Path) -> None:
        capture = _read_capture(tmp_path, "no-such-trace")

        assert _load_memory_state(capture) == _MEMORY_STATE_UNKNOWN

    def test_nothing_relevant_is_read_verbatim(self, tmp_path: pathlib.Path) -> None:
        _write_capture(tmp_path, "t-absent", {"memory_state": "nothing_relevant", "items": []})

        capture = _read_capture(tmp_path, "t-absent")

        assert _load_memory_state(capture) == "nothing_relevant"

    def test_unavailable_is_distinguishable_from_nothing_relevant(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The exact collapse AC-7 exists to end: both states render zero items, so the
        state must come from the record's own field, not from ``items`` being empty.
        """
        _write_capture(tmp_path, "t-a", {"memory_state": "nothing_relevant", "items": []})
        _write_capture(tmp_path, "t-b", {"memory_state": "unavailable", "items": []})

        state_a = _load_memory_state(_read_capture(tmp_path, "t-a"))
        state_b = _load_memory_state(_read_capture(tmp_path, "t-b"))

        assert state_a == "nothing_relevant"
        assert state_b == "unavailable"
        assert state_a != state_b

    def test_a_capture_from_before_this_field_existed_is_unknown(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A pre-FRE-1478 capture's ``recall_admission`` carries no ``memory_state`` key."""
        _write_capture(tmp_path, "t-legacy", {"items": ["Sailing"]})

        assert _load_memory_state(_read_capture(tmp_path, "t-legacy")) == _MEMORY_STATE_UNKNOWN


class TestLoadRenderedMemoryUnchanged:
    """The AC-5 helper this refactor shares a capture reader with, unaffected."""

    def test_missing_capture_still_reports_missing(self, tmp_path: pathlib.Path) -> None:
        capture = _read_capture(tmp_path, "no-such-trace")

        assert _load_rendered_memory(capture) == (_CAPTURE_MISSING,)

    def test_items_still_render_as_before(self, tmp_path: pathlib.Path) -> None:
        _write_capture(
            tmp_path,
            "t-1",
            {"items": [{"identity": "Sailing", "score": 0.8}], "memory_state": "populated"},
        )

        capture = _read_capture(tmp_path, "t-1")

        assert _load_rendered_memory(capture) == ("Sailing (score=0.8)",)
