"""The session-digest capture reader reads the durable store (FRE-992, ADR-0124 AC-8).

The producer used to read a single on-disk directory resolved by walking up from its
own module file. That directory is not durable in the gateway container, so 46 sessions
carrying 2-17 turns were read as empty and permanently retired — while their captures
sat in Elasticsearch the whole time.

Two properties are asserted here, and they are the ones the defect turned on:

* **Both stores are read and unioned.** They are not replicas. ``write_capture`` writes
  disk synchronously and then *schedules* the ES index fire-and-forget, so either store
  can hold a turn the other lacks. Reading whichever one answers first is what hid a
  turn forever.
* **An unreadable doc or file makes the read non-authoritative**, rather than silently
  shrinking the transcript. A reader that drops a malformed capture and reports success
  hands the summariser a gap it will read as evidence of absence.

No live Elasticsearch: every test injects a stub client (FRE-375).
"""

# ruff: noqa: D103

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import orjson
import pytest

from personal_agent.captains_log import capture as capture_mod
from personal_agent.captains_log.capture import (
    CAPTURES_INDEX_PREFIX,
    SUBAGENT_CAPTURES_INDEX_PREFIX,
    CaptureSource,
    TaskCapture,
    load_session_captures,
)
from personal_agent.captains_log.es_indexer import normalize_capture_doc_for_es

_USER_ID = uuid4()
_SESSION = "sess-992"
_STARTED = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
_ENDED = datetime(2026, 7, 20, 11, 0, tzinfo=timezone.utc)


def _capture(
    n: int,
    *,
    session_id: str = _SESSION,
    tool_results: list[dict[str, Any]] | None = None,
) -> TaskCapture:
    return TaskCapture(
        trace_id=f"trace-{n:03d}",
        session_id=session_id,
        timestamp=_STARTED + timedelta(minutes=n),
        user_message=f"question {n}",
        assistant_response=f"answer {n}",
        outcome="completed",
        user_id=_USER_ID,
        tool_results=tool_results or [],
    )


def _es_doc(capture: TaskCapture) -> dict[str, Any]:
    """A doc exactly as ``write_capture`` would have indexed it."""
    return normalize_capture_doc_for_es(capture.model_dump(mode="json"))


class _FakeES:
    """Minimal AsyncElasticsearch stand-in for the capture read path."""

    def __init__(
        self,
        *,
        docs: list[dict[str, Any]] | None = None,
        raise_on_search: Exception | None = None,
    ) -> None:
        self.docs = docs or []
        self.raise_on_search = raise_on_search
        self.last_call: dict[str, Any] = {}

    async def search(self, **kwargs: Any) -> dict[str, Any]:
        self.last_call = kwargs
        if self.raise_on_search is not None:
            raise self.raise_on_search
        size = kwargs.get("size", 10_000)
        return {"hits": {"hits": [{"_source": d} for d in self.docs[:size]]}}


@pytest.fixture
def empty_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the on-disk reader at an empty temp directory."""
    captures_dir = tmp_path / "captures"
    captures_dir.mkdir()
    monkeypatch.setattr(capture_mod, "_get_captures_dir", lambda: captures_dir)
    return captures_dir


def _write_to_disk(captures_dir: Path, capture: TaskCapture) -> Path:
    date_dir = captures_dir / capture.timestamp.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    path = date_dir / f"{capture.trace_id}.json"
    path.write_text(orjson.dumps(capture.model_dump(mode="json")).decode(), encoding="utf-8")
    return path


async def _load(es: _FakeES | None, *, limit: int = 1000) -> Any:
    return await load_session_captures(
        _SESSION,
        started_at=_STARTED,
        ended_at=_ENDED,
        es_client=es,
        limit=limit,
    )


# --------------------------------------------------------------------------
# The durable store is read at all — the defect itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reads_a_session_whose_captures_exist_only_in_elasticsearch(
    empty_disk: Path,
) -> None:
    """The FRE-992 case: nothing on disk, everything in ES."""
    es = _FakeES(docs=[_es_doc(_capture(n)) for n in range(1, 18)])

    read = await _load(es)

    assert len(read.captures) == 17, "the durable store holds these; the disk directory does not"
    assert read.source is CaptureSource.ELASTICSEARCH
    assert read.complete is True
    assert read.unreadable == 0


@pytest.mark.asyncio
async def test_query_scopes_to_the_session_and_excludes_the_subagent_sibling_index(
    empty_disk: Path,
) -> None:
    """The sub-agent audit index is a sibling under the same wildcard (capture.py:27).

    Its docs carry ``session_id`` but a ``SubAgentCapture`` shape, so an unexcluded
    query returns N of them per turn — each of which fails ``TaskCapture`` validation
    and would land in ``unreadable``, making every read non-authoritative.
    """
    es = _FakeES(docs=[_es_doc(_capture(1))])

    await _load(es)

    index = es.last_call["index"]
    assert index.startswith(f"{CAPTURES_INDEX_PREFIX}-*")
    assert f"-{SUBAGENT_CAPTURES_INDEX_PREFIX}-*" in index, "sub-agent index not excluded"

    filters = es.last_call["query"]["bool"]["filter"]
    assert {"term": {"session_id": _SESSION}} in filters
    # Sorted ascending with a deterministic tie-break — two captures sharing a
    # timestamp must not reorder between reads.
    assert es.last_call["sort"] == [{"timestamp": "asc"}, {"trace_id": "asc"}]


@pytest.mark.asyncio
async def test_other_sessions_in_the_window_are_not_returned(empty_disk: Path) -> None:
    """The stub ignores the query, so this asserts the post-filter, not the term clause."""
    es = _FakeES(docs=[_es_doc(_capture(1)), _es_doc(_capture(2, session_id="other-session"))])

    read = await _load(es)

    assert [c.trace_id for c in read.captures] == ["trace-001"]


# --------------------------------------------------------------------------
# Union — the stores are not replicas
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complementary_halves_union_to_the_whole_session(empty_disk: Path) -> None:
    """ES holds turns 1 and 3; disk holds turn 2. Reading either alone loses a turn.

    ``write_capture`` writes disk synchronously and schedules the ES index
    fire-and-forget (``es_indexer.py`` drops the task when no loop is running), so this
    split is a real state, not a contrived one.
    """
    _write_to_disk(empty_disk, _capture(2))
    es = _FakeES(docs=[_es_doc(_capture(1)), _es_doc(_capture(3))])

    read = await _load(es)

    assert [c.trace_id for c in read.captures] == ["trace-001", "trace-002", "trace-003"]
    assert read.source is CaptureSource.BOTH
    assert read.complete is True


@pytest.mark.asyncio
async def test_a_capture_in_both_stores_is_returned_once(empty_disk: Path) -> None:
    _write_to_disk(empty_disk, _capture(1))
    _write_to_disk(empty_disk, _capture(2))
    es = _FakeES(docs=[_es_doc(_capture(1)), _es_doc(_capture(2))])

    read = await _load(es)

    assert [c.trace_id for c in read.captures] == ["trace-001", "trace-002"]


@pytest.mark.asyncio
async def test_falls_back_to_disk_when_elasticsearch_raises(empty_disk: Path) -> None:
    """A store that could not be consulted makes the read non-authoritative.

    The captures found on disk are still returned — they are real — but the read must
    not claim completeness while the durable store is unreachable.
    """
    _write_to_disk(empty_disk, _capture(1))
    _write_to_disk(empty_disk, _capture(2))
    es = _FakeES(raise_on_search=RuntimeError("connection refused"))

    read = await _load(es)

    assert [c.trace_id for c in read.captures] == ["trace-001", "trace-002"]
    assert read.source is CaptureSource.DISK
    assert read.complete is False, "the durable store was not consulted"


@pytest.mark.asyncio
async def test_no_elasticsearch_client_reads_disk_only_and_is_never_complete(
    empty_disk: Path,
) -> None:
    """No client is ever constructed here (FRE-375) — absent means disk-only.

    And a disk-only read is never authoritative. The client is resolved once at
    startup, so it is None for the whole process when Elasticsearch was down at boot;
    treating that as a proven-complete read would classify a total outage as a
    *deterministic* shortfall and spend every session's retry budget on it.
    """
    _write_to_disk(empty_disk, _capture(1))

    read = await _load(None)

    assert [c.trace_id for c in read.captures] == ["trace-001"]
    assert read.source is CaptureSource.DISK
    assert read.complete is False
    assert read.stores_unavailable is True, "not having asked is not having been told"


@pytest.mark.asyncio
async def test_both_stores_empty_is_a_readable_but_empty_result(empty_disk: Path) -> None:
    read = await _load(_FakeES())

    assert read.captures == ()
    assert read.source is CaptureSource.NONE
    # Nothing failed to parse: the stores genuinely hold nothing. Whether that is a
    # one-turn session or lost evidence is decided against the graph, not here.
    assert read.complete is True


# --------------------------------------------------------------------------
# Unreadable evidence is counted, never swallowed
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unparseable_elasticsearch_doc_makes_the_read_non_authoritative(
    empty_disk: Path,
) -> None:
    es = _FakeES(docs=[_es_doc(_capture(1)), {"trace_id": "trace-002", "session_id": _SESSION}])

    read = await _load(es)

    assert len(read.captures) == 1
    assert read.unreadable == 1
    assert read.complete is False


@pytest.mark.asyncio
async def test_an_unreadable_disk_file_makes_the_read_non_authoritative(
    empty_disk: Path,
) -> None:
    """``read_captures`` logs and drops a malformed file, reporting nothing.

    A reader that silently shrinks the transcript hands the summariser a gap it is
    explicitly instructed to read as evidence of absence.

    The file here is valid JSON that fails ``TaskCapture`` validation — schema drift,
    a field this version rejects. That is the attributable case: it names its own
    session, so the hole is provably *this* session's.
    """
    _write_to_disk(empty_disk, _capture(1))
    broken = _write_to_disk(empty_disk, _capture(2))
    broken.write_text(
        orjson.dumps({"session_id": _SESSION, "trace_id": "trace-002"}).decode(),
        encoding="utf-8",
    )

    read = await _load(_FakeES())

    assert [c.trace_id for c in read.captures] == ["trace-001"]
    assert read.unreadable == 1
    assert read.complete is False


@pytest.mark.asyncio
async def test_a_corrupt_file_belonging_to_another_session_is_not_charged_here(
    empty_disk: Path,
) -> None:
    """A date directory holds every session's captures for that day.

    The filename is a trace_id, so an unattributed failure count would let one corrupt
    file mark the read of every *other* session in the window non-authoritative — and
    each of those healthy sessions would then spend its retry budget on a hole that was
    never theirs.
    """
    _write_to_disk(empty_disk, _capture(1))
    _write_to_disk(empty_disk, _capture(2))
    stranger = _write_to_disk(empty_disk, _capture(3, session_id="someone-elses-session"))
    stranger.write_text(
        '{"session_id": "someone-elses-session", "trace_id": "x"}', encoding="utf-8"
    )

    read = await _load(_FakeES())

    assert [c.trace_id for c in read.captures] == ["trace-001", "trace-002"]
    assert read.unreadable == 0, "another session's hole is not this session's"
    assert read.unattributable == 0, "it names its own session, and it is not ours"
    assert read.complete is True


@pytest.mark.asyncio
async def test_an_unattributable_file_is_reported_but_not_charged(empty_disk: Path) -> None:
    """Not even valid JSON, so its session cannot be determined.

    Charging it would condemn every session in the window; ignoring it silently would
    hide it. It is logged loudly and left uncharged — the honest position when the
    file cannot be shown to belong to anyone.
    """
    _write_to_disk(empty_disk, _capture(1))
    _write_to_disk(empty_disk, _capture(2))
    orphan = _write_to_disk(empty_disk, _capture(3))
    orphan.write_text("{not json at all", encoding="utf-8")

    read = await _load(_FakeES())

    assert [c.trace_id for c in read.captures] == ["trace-001", "trace-002"]
    assert read.unreadable == 0, "it cannot be pinned on this session"
    assert read.unattributable == 1, "but it is surfaced, not swallowed"
    assert read.complete is True


@pytest.mark.asyncio
async def test_an_orphan_file_the_durable_store_already_holds_is_discharged(
    empty_disk: Path,
) -> None:
    """The filename is the capture's trace_id.

    A file truncated mid-write is provably not a hole when its Elasticsearch twin is
    in the result — the capture is present, only its local copy is damaged.
    """
    orphan = _write_to_disk(empty_disk, _capture(1))
    orphan.write_text("{truncated mid-writ", encoding="utf-8")
    es = _FakeES(docs=[_es_doc(_capture(1)), _es_doc(_capture(2))])

    read = await _load(es)

    assert [c.trace_id for c in read.captures] == ["trace-001", "trace-002"]
    assert read.unattributable == 0, "the durable store accounts for trace-001"
    assert read.complete is True


@pytest.mark.asyncio
async def test_an_unreachable_store_is_reported_separately_from_a_corrupt_one(
    empty_disk: Path,
) -> None:
    """The two have opposite retry semantics and must not be collapsed.

    An outage is transient — the same read may succeed on the next sweep. A corrupt
    document fails the same way forever. Charging an outage to a retry bound is what
    would let a brief Elasticsearch restart retire a session permanently.
    """
    _write_to_disk(empty_disk, _capture(1))
    es = _FakeES(raise_on_search=RuntimeError("connection refused"))

    read = await _load(es)

    assert read.stores_unavailable is True
    assert read.unreadable == 0, "an outage is not a corrupt document"
    assert read.complete is False

    healthy = await _load(_FakeES(docs=[_es_doc(_capture(1))]))
    assert healthy.stores_unavailable is False


@pytest.mark.asyncio
async def test_hitting_the_size_limit_makes_the_read_non_authoritative(
    empty_disk: Path,
) -> None:
    """A fixed ``size`` is itself a truncation boundary; exhaustion is unproven at it."""
    es = _FakeES(docs=[_es_doc(_capture(n)) for n in range(1, 6)])

    read = await _load(es, limit=3)

    assert len(read.captures) == 3
    assert read.complete is False


# --------------------------------------------------------------------------
# Round-trip: what write_capture indexes must validate as what we read back
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output",
    [
        {"rows": [1, 2, 3]},
        ["a", "b"],
        None,
        "already a string",
    ],
)
@pytest.mark.asyncio
async def test_normalized_tool_results_round_trip_back_into_a_capture(
    empty_disk: Path, output: object
) -> None:
    """Locks the writer's on-the-wire format to this reader.

    ``normalize_capture_doc_for_es`` JSON-serialises non-string ``tool_results.output``
    and ``arguments`` before indexing. A reader that could not validate its own writer's
    output would fail every doc into ``unreadable`` and never digest anything.
    """
    original = _capture(
        1, tool_results=[{"tool_name": "search", "success": True, "output": output}]
    )
    es = _FakeES(docs=[_es_doc(original)])

    read = await _load(es)

    assert read.unreadable == 0
    assert read.complete is True
    (restored,) = read.captures
    assert restored.trace_id == original.trace_id
    assert restored.user_message == original.user_message
    assert restored.assistant_response == original.assistant_response
    assert restored.tool_results[0]["tool_name"] == "search"
