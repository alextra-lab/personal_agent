"""FRE-1122 — cleanup's scope is the run, not a session, and it reaches Postgres.

Two defects are under test here, and both are the kind that make the fixture
*look* like it worked.

**The run is the cleanup unit.** Splitting the twenty probes into twenty sessions
(so no probe is answered inside another's history) changes what "outside this
session" means. ``_DELETE_ENTITIES`` retains any probe-created entity a turn
outside the session discusses, so under a per-probe split, probe 3's entity
mentioned on probe 7's turn becomes residue the split itself manufactured. Every
predicate therefore scopes to the run's whole session set.

**Widening the binding guard must not weaken it.** The guard refuses a session it
cannot prove belongs to this run. Aggregated over twenty ids, an id contributing
zero turns stops failing, because the other nineteen carry the aggregate past
``turns > 0`` — and that id then reaches the delete. So the guard is per id.

**Cleanup must reach Postgres.** ``gather_evidence`` establishes absence against
the graph *and* ``sessions.messages``. Cleanup that deletes only graph nodes
leaves every absent probe's own question in the message history, so the third
evidence pass never returns to zero and AC-3 reports irreversibility that is a
gap in cleanup rather than a property of the substrate.

No substrate is touched (FRE-375): the driver and the connection are recording
fakes, which is also what makes the *scoping* assertable at all.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest
from scripts.eval.fre1122_absence_probe.ground_truth import (
    CleanupRefused,
    cleanup_probe_session,
)

_USER = "11111111-1111-1111-1111-111111111111"
_SIDS = ("aaaa1111-0000-0000-0000-000000000001", "bbbb2222-0000-0000-0000-000000000002")
_TRACES = ("trace-a", "trace-b")


# ── Recording fakes ───────────────────────────────────────────────────────────


class _FakeResult:
    """A Neo4j result over pre-canned rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def single(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    async def data(self) -> list[dict[str, Any]]:
        return list(self._rows)

    async def consume(self) -> None:
        return None


class _RecordingSession:
    """Records every statement and parameter set the cleanup runs."""

    def __init__(self, *, bound_sids: tuple[str, ...] = _SIDS) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._bound = bound_sids

    async def run(self, statement: str, **params: Any) -> _FakeResult:
        self.calls.append((statement, params))
        return _FakeResult(self._rows_for(statement, params))

    def _rows_for(self, statement: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "UNWIND $sids" in statement:
            # One row per id the guard asked about. An id outside `bound_sids`
            # reports zero turns, which is how a test seeds an unbound session.
            return [
                {
                    "session_id": sid,
                    "turns": 1 if sid in self._bound else 0,
                    "owned": 1 if sid in self._bound else 0,
                    "matching_traces": 1 if sid in self._bound else 0,
                }
                for sid in params.get("sids", [])
            ]
        if "collect(cl.claim_id)" in statement:
            return [{"claim_ids": []}]
        for key in ("mutated", "filled", "rewritten", "restored"):
            if f"AS {key}" in statement:
                return [{key: 0}]
        return []

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


class _RecordingDriver:
    def __init__(self, session: _RecordingSession) -> None:
        self._session = session

    def session(self) -> _RecordingSession:
        return self._session


class _RecordingPg:
    """A recording asyncpg connection over pre-canned session rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = (
            rows
            if rows is not None
            else [{"session_id": sid, "user_id": _USER, "messages": "[]"} for sid in _SIDS]
        )
        self.fetches: list[tuple[str, tuple[Any, ...]]] = []
        self.executes: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, statement: str, *args: Any) -> list[dict[str, Any]]:
        self.fetches.append((statement, args))
        return self.rows

    async def execute(self, statement: str, *args: Any) -> str:
        self.executes.append((statement, args))
        return f"DELETE {len(self.rows)}"


async def _run_cleanup(
    tmp_path: pathlib.Path,
    *,
    session_ids: tuple[str, ...] = _SIDS,
    bound_sids: tuple[str, ...] = _SIDS,
    pg: _RecordingPg | None = None,
    dry_run: bool = False,
) -> tuple[Any, _RecordingSession, _RecordingPg]:
    """Drive one cleanup against recording fakes."""
    session = _RecordingSession(bound_sids=bound_sids)
    conn = pg if pg is not None else _RecordingPg()
    result = await cleanup_probe_session(
        _RecordingDriver(session),  # type: ignore[arg-type]
        session_ids,
        user_id=_USER,
        snapshot_path=tmp_path / "snapshot.jsonl",
        pg_conn=conn,  # type: ignore[arg-type]
        trace_ids=_TRACES,
        dry_run=dry_run,
    )
    return result, session, conn


# ── The run is the cleanup unit ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_scopes_to_every_probe_session(tmp_path: pathlib.Path) -> None:
    """Every statement binds the whole session set, never a single session.

    A statement still carrying a scalar ``sid`` would scope that step to one
    probe and leave the other nineteen behind.
    """
    _, session, _ = await _run_cleanup(tmp_path)

    assert session.calls, "cleanup ran no statements"
    for statement, params in session.calls:
        assert "sid" not in params, f"a scalar session parameter survived: {statement[:60]}"
        if "$sids" in statement:
            assert list(params["sids"]) == list(_SIDS)


@pytest.mark.asyncio
async def test_intra_run_adoption_is_not_counted_as_residue(tmp_path: pathlib.Path) -> None:
    """A turn inside the run is not "outside the session".

    This is the residue the split would otherwise manufacture: the entity
    predicates must exclude turns belonging to *any* of the run's sessions, not
    just the one that created the entity.
    """
    _, session, _ = await _run_cleanup(tmp_path)

    entity_statements = [s for s, _ in session.calls if "originating_session_id" in s]
    assert entity_statements, "no entity-scoped statement ran"
    for statement in entity_statements:
        assert "= $sid" not in statement, "an entity predicate still pins one session"
        if "NOT EXISTS" in statement or "EXISTS {" in statement:
            assert "IN $sids" in statement


# ── Widening must not weaken the destructive guard ────────────────────────────


@pytest.mark.asyncio
async def test_a_single_unbound_session_id_refuses_the_whole_cleanup(
    tmp_path: pathlib.Path,
) -> None:
    """One id the run cannot prove is enough to refuse everything.

    Aggregated over the set, an id with zero turns rides on the others and then
    reaches ``_DELETE_ENTITIES``, whose ``IN $sids`` can match entities the run
    never created. The guard is therefore per id.
    """
    stranger = "cccc3333-0000-0000-0000-000000000003"

    with pytest.raises(CleanupRefused) as excinfo:
        await _run_cleanup(
            tmp_path,
            session_ids=(*_SIDS, stranger),
            bound_sids=_SIDS,
        )

    assert stranger in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_dry_run_deletes_nothing_anywhere(tmp_path: pathlib.Path) -> None:
    """A dry run is not cleanup — in either substrate."""
    _, session, conn = await _run_cleanup(tmp_path, dry_run=True)

    assert not [s for s, _ in session.calls if "DELETE" in s.upper()]
    assert conn.executes == []


# ── Cleanup reaches Postgres (Defect D) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_removes_the_probe_sessions_message_rows(
    tmp_path: pathlib.Path,
) -> None:
    """The absent half cannot return to zero while the questions remain.

    ``gather_evidence`` reads ``sessions.messages``; graph-only cleanup leaves
    every absent probe's own question there, so AC-3 would report
    irreversibility that is really a gap in cleanup.
    """
    result, _, conn = await _run_cleanup(tmp_path)

    assert conn.executes, "cleanup never deleted the probe sessions' rows"
    statement, args = conn.executes[0]
    assert "DELETE FROM sessions" in statement
    assert list(args[0]) == list(_SIDS)
    assert result.message_rows_removed == len(_SIDS)


@pytest.mark.asyncio
async def test_the_session_rows_are_snapshotted_before_deletion(
    tmp_path: pathlib.Path,
) -> None:
    """Durable before destructive, on the relational side too."""
    snapshot = tmp_path / "snapshot.jsonl"
    result, _, conn = await _run_cleanup(tmp_path)

    assert conn.fetches, "the session rows were deleted without being read first"
    entries = [json.loads(line) for line in snapshot.read_text().splitlines()]
    labels = {entry["label"] for entry in entries}
    assert "SessionRow" in labels, "the message history has no undo record"
    assert result.snapshot_path == snapshot


@pytest.mark.asyncio
async def test_a_foreign_session_row_refuses_cleanup(tmp_path: pathlib.Path) -> None:
    """A session row owned by someone else is never this run's to delete."""
    foreign = _RecordingPg(
        rows=[
            {"session_id": _SIDS[0], "user_id": _USER, "messages": "[]"},
            {
                "session_id": _SIDS[1],
                "user_id": "99999999-9999-9999-9999-999999999999",
                "messages": "[]",
            },
        ]
    )

    with pytest.raises(CleanupRefused):
        await _run_cleanup(tmp_path, pg=foreign)

    assert foreign.executes == [], "a refused cleanup still deleted rows"


@pytest.mark.asyncio
async def test_a_missing_session_row_refuses_cleanup(tmp_path: pathlib.Path) -> None:
    """Every requested session must be accounted for on both sides."""
    partial = _RecordingPg(rows=[{"session_id": _SIDS[0], "user_id": _USER, "messages": "[]"}])

    with pytest.raises(CleanupRefused):
        await _run_cleanup(tmp_path, pg=partial)

    assert partial.executes == []
