"""``build_capture_index`` / ``read_captures_by_trace_ids`` (ADR-0098 A5, FRE-1348).

The provenance backfill needs to resolve a legacy item's *minting capture* by trace_id —
disk first, Elasticsearch as a fallback for whatever disk misses, since neither store is
a replica of the other (``write_capture`` writes disk synchronously but schedules the ES
index as a best-effort fire-and-forget task). No live Elasticsearch: every test injects a
stub client (FRE-375).
"""

# ruff: noqa: D103

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import orjson
import pytest

from personal_agent.captains_log import capture as capture_mod
from personal_agent.captains_log.capture import (
    CAPTURES_INDEX_PREFIX,
    SUBAGENT_CAPTURES_INDEX_PREFIX,
    TaskCapture,
    build_capture_index,
    read_captures_by_trace_ids,
)
from personal_agent.captains_log.es_indexer import normalize_capture_doc_for_es

_USER_ID = uuid4()
_TS = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)


def _capture(trace_id: str, *, timestamp: datetime = _TS) -> TaskCapture:
    return TaskCapture(
        trace_id=trace_id,
        session_id="sess-1348",
        timestamp=timestamp,
        user_message="q",
        assistant_response="a",
        outcome="completed",
        user_id=_USER_ID,
    )


def _write_to_disk(captures_dir: Path, capture: TaskCapture) -> Path:
    date_dir = captures_dir / capture.timestamp.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    path = date_dir / f"{capture.trace_id}.json"
    path.write_text(orjson.dumps(capture.model_dump(mode="json")).decode(), encoding="utf-8")
    return path


class _FakeES:
    """Minimal AsyncElasticsearch stand-in, mirroring test_session_capture_source.py."""

    def __init__(
        self, *, docs: list[dict[str, Any]] | None = None, raise_on_search: Exception | None = None
    ) -> None:
        self.docs = docs or []
        self.raise_on_search = raise_on_search
        self.calls: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.raise_on_search is not None:
            raise self.raise_on_search
        return {"hits": {"hits": [{"_source": d} for d in self.docs]}}


@pytest.fixture
def captures_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "captures"
    d.mkdir()
    monkeypatch.setattr(capture_mod, "_get_captures_dir", lambda: d)
    return d


def test_build_capture_index_spans_multiple_date_directories(captures_dir: Path) -> None:
    c1 = _capture("trace-a", timestamp=_TS)
    c2 = _capture("trace-b", timestamp=_TS.replace(day=30))
    _write_to_disk(captures_dir, c1)
    _write_to_disk(captures_dir, c2)

    index = build_capture_index()

    assert set(index) == {"trace-a", "trace-b"}
    assert index["trace-a"].name == "trace-a.json"


def test_build_capture_index_empty_when_dir_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture_mod, "_get_captures_dir", lambda: tmp_path / "nonexistent")
    assert build_capture_index() == {}


@pytest.mark.asyncio
async def test_resolves_from_disk_without_touching_es(captures_dir: Path) -> None:
    c = _capture("trace-disk")
    _write_to_disk(captures_dir, c)
    index = build_capture_index()
    es = _FakeES(docs=[])

    resolved = await read_captures_by_trace_ids(["trace-disk"], disk_index=index, es_client=es)

    assert resolved["trace-disk"].trace_id == "trace-disk"
    assert es.calls == [], "found on disk — no ES round trip should have been made"


@pytest.mark.asyncio
async def test_unknown_trace_id_absent_from_result_no_exception(captures_dir: Path) -> None:
    index = build_capture_index()

    resolved = await read_captures_by_trace_ids(
        ["missing-everywhere"], disk_index=index, es_client=None
    )

    assert resolved == {}


@pytest.mark.asyncio
async def test_es_client_none_skips_es_entirely_for_disk_misses(captures_dir: Path) -> None:
    index = build_capture_index()

    resolved = await read_captures_by_trace_ids(["not-on-disk"], disk_index=index, es_client=None)

    assert resolved == {}


@pytest.mark.asyncio
async def test_corrupt_disk_file_excluded_other_batch_members_still_resolve(
    captures_dir: Path,
) -> None:
    good = _capture("trace-good")
    _write_to_disk(captures_dir, good)
    bad_path = captures_dir / _TS.strftime("%Y-%m-%d") / "trace-bad.json"
    bad_path.write_text("not json{{{", encoding="utf-8")
    index = build_capture_index()

    resolved = await read_captures_by_trace_ids(
        ["trace-good", "trace-bad"], disk_index=index, es_client=None
    )

    assert "trace-good" in resolved
    assert "trace-bad" not in resolved


def test_pre_fre343_file_parses_via_nil_uuid_injection(captures_dir: Path) -> None:
    date_dir = captures_dir / _TS.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True)
    raw = _capture("trace-legacy").model_dump(mode="json")
    raw["user_id"] = None
    (date_dir / "trace-legacy.json").write_text(orjson.dumps(raw).decode(), encoding="utf-8")
    index = build_capture_index()

    parsed = capture_mod._parse_capture_file(index["trace-legacy"])

    assert parsed is not None
    assert str(parsed.user_id) == "00000000-0000-0000-0000-000000000000"


@pytest.mark.asyncio
async def test_es_fallback_resolves_disk_misses_only(captures_dir: Path) -> None:
    on_disk = _capture("trace-disk-hit")
    _write_to_disk(captures_dir, on_disk)
    only_in_es = _capture("trace-es-hit")
    index = build_capture_index()
    es = _FakeES(docs=[normalize_capture_doc_for_es(only_in_es.model_dump(mode="json"))])

    resolved = await read_captures_by_trace_ids(
        ["trace-disk-hit", "trace-es-hit"], disk_index=index, es_client=es
    )

    assert set(resolved) == {"trace-disk-hit", "trace-es-hit"}
    assert len(es.calls) == 1, "one batched query, not one per trace_id"
    assert es.calls[0]["query"] == {"ids": {"values": ["trace-es-hit"]}}, (
        "the query must scope to the disk-miss only, not the whole requested batch"
    )
    index_pattern = es.calls[0]["index"]
    assert index_pattern.startswith(f"{CAPTURES_INDEX_PREFIX}-*")
    assert f"-{SUBAGENT_CAPTURES_INDEX_PREFIX}-*" in index_pattern


@pytest.mark.asyncio
async def test_es_outage_degrades_to_disk_only_result(captures_dir: Path) -> None:
    on_disk = _capture("trace-disk-hit")
    _write_to_disk(captures_dir, on_disk)
    index = build_capture_index()
    es = _FakeES(raise_on_search=RuntimeError("es down"))

    resolved = await read_captures_by_trace_ids(
        ["trace-disk-hit", "trace-es-only"], disk_index=index, es_client=es
    )

    assert set(resolved) == {"trace-disk-hit"}
