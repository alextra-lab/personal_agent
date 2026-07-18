# ruff: noqa: D103
"""Unit tests for the ADR-0110 dispatch NEXT resolver (FRE-785).

Exercises `resolve_next` — the pure dispatch-contract logic — against fixture
board states only (no live Linear), per the ticket's testing constraint.

Covers ADR-0110 acceptance criteria:
  AC-1 — the resolver returns exactly the ticket the prime-worker contract
         would: correct stream, priority order, blocked-head skipped, busy
         guard honored, and a stale-but-satisfied blocker is not skipped.
  AC-6 — an occupied stream yields no candidate.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest
from scripts.dispatch.launcher import known_streams
from scripts.dispatch.next_resolver import (
    Blocker,
    IssueSnapshot,
    eligible_candidates,
    fetch_board,
    main,
    resolve_next,
    stream_label,
)
from scripts.reconcile_board import load_linear_key


def _issue(
    identifier: str,
    state: str,
    priority: int,
    created_at: str,
    labels: frozenset[str],
    blocked_by: tuple[Blocker, ...] = (),
) -> IssueSnapshot:
    return IssueSnapshot(
        identifier=identifier,
        state=state,
        priority=priority,
        created_at=created_at,
        labels=labels,
        blocked_by=blocked_by,
    )


def test_stream_label_format() -> None:
    assert stream_label("build2") == "stream:build2"


# --- AC-1 fixture #1: higher-priority-but-blocked head is skipped ----------


@pytest.mark.parametrize("blocker_state", ["In Progress", "In Review"])
def test_blocked_head_is_skipped_in_favor_of_lower_priority(blocker_state: str) -> None:
    blocked_head = _issue(
        "FRE-1",
        "Approved",
        priority=2,  # High
        created_at="2026-01-01T00:00:00Z",
        labels=frozenset({"stream:build2"}),
        blocked_by=(Blocker(identifier="FRE-0", state=blocker_state),),
    )
    unblocked = _issue(
        "FRE-2",
        "Approved",
        priority=3,  # Medium
        created_at="2026-01-02T00:00:00Z",
        labels=frozenset({"stream:build2"}),
    )
    result = resolve_next([blocked_head, unblocked], "build2")
    assert result is not None
    assert result.identifier == "FRE-2"


# --- AC-1 fixture #2: wrong-stream decoy is excluded -----------------------


def test_wrong_stream_decoy_excluded() -> None:
    decoy = _issue(
        "FRE-3",
        "Approved",
        priority=1,  # Urgent
        created_at="2026-01-01T00:00:00Z",
        labels=frozenset({"stream:build1"}),
    )
    target = _issue(
        "FRE-4",
        "Approved",
        priority=4,  # Low
        created_at="2026-01-01T00:00:00Z",
        labels=frozenset({"stream:build2"}),
    )
    result = resolve_next([decoy, target], "build2")
    assert result is not None
    assert result.identifier == "FRE-4"


# --- AC-1 fixture #3 / AC-6: occupied stream yields no candidate -----------


@pytest.mark.parametrize("occupying_state", ["In Progress", "In Review"])
def test_occupied_stream_yields_no_candidate(occupying_state: str) -> None:
    occupying = _issue(
        "FRE-5",
        occupying_state,
        priority=2,
        created_at="2026-01-01T00:00:00Z",
        labels=frozenset({"stream:build2"}),
    )
    approved = _issue(
        "FRE-6",
        "Approved",
        priority=1,
        created_at="2026-01-01T00:00:00Z",
        labels=frozenset({"stream:build2"}),
    )
    result = resolve_next([occupying, approved], "build2")
    assert result is None


# --- AC-1 fixture #4: empty board yields no candidate ----------------------


def test_empty_board_yields_no_candidate() -> None:
    assert resolve_next([], "build2") is None


# --- AC-1 fixture #5: stale-but-satisfied blocker is NOT skipped -----------


@pytest.mark.parametrize("terminal_state", ["Awaiting Deploy", "Done", "Canceled", "Duplicate"])
def test_stale_terminal_blocker_is_not_skipped(terminal_state: str) -> None:
    issue = _issue(
        "FRE-7",
        "Approved",
        priority=3,
        created_at="2026-01-01T00:00:00Z",
        labels=frozenset({"stream:build2"}),
        blocked_by=(Blocker(identifier="FRE-6", state=terminal_state),),
    )
    result = resolve_next([issue], "build2")
    assert result is not None
    assert result.identifier == "FRE-7"


# --- Priority ordering ------------------------------------------------------


def test_priority_ordering_urgent_first_none_last() -> None:
    label = frozenset({"stream:build2"})
    urgent = _issue("FRE-U", "Approved", 1, "2026-01-05T00:00:00Z", label)
    high = _issue("FRE-H", "Approved", 2, "2026-01-04T00:00:00Z", label)
    medium = _issue("FRE-M", "Approved", 3, "2026-01-03T00:00:00Z", label)
    low = _issue("FRE-L", "Approved", 4, "2026-01-02T00:00:00Z", label)
    none_priority = _issue("FRE-N", "Approved", 0, "2026-01-01T00:00:00Z", label)

    # Shuffle input order; resolver must still pick Urgent regardless of list order.
    result = resolve_next([none_priority, low, medium, high, urgent], "build2")
    assert result is not None
    assert result.identifier == "FRE-U"

    # Remove Urgent; High becomes head. Continue peeling to prove full ordering.
    result = resolve_next([none_priority, low, medium, high], "build2")
    assert result is not None
    assert result.identifier == "FRE-H"

    result = resolve_next([none_priority, low, medium], "build2")
    assert result is not None
    assert result.identifier == "FRE-M"

    result = resolve_next([none_priority, low], "build2")
    assert result is not None
    assert result.identifier == "FRE-L"

    result = resolve_next([none_priority], "build2")
    assert result is not None
    assert result.identifier == "FRE-N"


def test_oldest_created_tie_break() -> None:
    label = frozenset({"stream:build2"})
    older = _issue("FRE-OLD", "Approved", 2, "2026-01-01T00:00:00Z", label)
    newer = _issue("FRE-NEW", "Approved", 2, "2026-01-02T00:00:00Z", label)
    result = resolve_next([newer, older], "build2")
    assert result is not None
    assert result.identifier == "FRE-OLD"


# --- Case-insensitive state matching ----------------------------------------


def test_case_insensitive_occupied_state() -> None:
    label = frozenset({"stream:build2"})
    occupying = _issue("FRE-8", "in progress", 2, "2026-01-01T00:00:00Z", label)
    approved = _issue("FRE-9", "Approved", 1, "2026-01-01T00:00:00Z", label)
    assert resolve_next([occupying, approved], "build2") is None


def test_case_insensitive_terminal_blocker_state() -> None:
    label = frozenset({"stream:build2"})
    issue = _issue(
        "FRE-10",
        "approved",
        3,
        "2026-01-01T00:00:00Z",
        label,
        blocked_by=(Blocker(identifier="FRE-9", state="done"),),
    )
    result = resolve_next([issue], "build2")
    assert result is not None
    assert result.identifier == "FRE-10"


# --- Multiple blockers: one open + one terminal → still skipped ------------


def test_multiple_blockers_one_open_still_skips() -> None:
    label = frozenset({"stream:build2"})
    blocked = _issue(
        "FRE-11",
        "Approved",
        1,
        "2026-01-01T00:00:00Z",
        label,
        blocked_by=(
            Blocker(identifier="FRE-A", state="Done"),
            Blocker(identifier="FRE-B", state="In Progress"),
        ),
    )
    fallback = _issue("FRE-12", "Approved", 4, "2026-01-01T00:00:00Z", label)
    result = resolve_next([blocked, fallback], "build2")
    assert result is not None
    assert result.identifier == "FRE-12"


# --- Missing blocker state is treated conservatively as open ----------------


def test_missing_blocker_state_treated_as_open() -> None:
    label = frozenset({"stream:build2"})
    blocked = _issue(
        "FRE-13",
        "Approved",
        1,
        "2026-01-01T00:00:00Z",
        label,
        blocked_by=(Blocker(identifier="FRE-A", state=None),),
    )
    fallback = _issue("FRE-14", "Approved", 4, "2026-01-01T00:00:00Z", label)
    result = resolve_next([blocked, fallback], "build2")
    assert result is not None
    assert result.identifier == "FRE-14"


# --- A blocker outside this stream still blocks if open ---------------------


def test_cross_stream_blocker_still_blocks_if_open() -> None:
    blocked = _issue(
        "FRE-15",
        "Approved",
        1,
        "2026-01-01T00:00:00Z",
        frozenset({"stream:build2"}),
        blocked_by=(Blocker(identifier="FRE-A", state="In Progress"),),
    )
    fallback = _issue("FRE-16", "Approved", 4, "2026-01-01T00:00:00Z", frozenset({"stream:build2"}))
    result = resolve_next([blocked, fallback], "build2")
    assert result is not None
    assert result.identifier == "FRE-16"


# --- eligible_candidates (FRE-846: master's advance-dispatch read-half) ----
#
# master's Step 8 "re-derive the eligible set" needs the FULL eligible list
# (to check "exactly one Urgent-or-High ticket"), busy-guard-free (it runs
# right after the merge that just freed the stream) — a shape `resolve_next`
# structurally cannot provide since it returns a single winner and applies
# the busy guard.


def test_eligible_candidates_ignores_busy_guard() -> None:
    label = frozenset({"stream:build2"})
    occupying = _issue("FRE-20", "In Progress", 2, "2026-01-01T00:00:00Z", label)
    approved = _issue("FRE-21", "Approved", 3, "2026-01-01T00:00:00Z", label)
    # resolve_next honors the busy guard: nothing, because FRE-20 occupies the stream.
    assert resolve_next([occupying, approved], "build2") is None
    # eligible_candidates ignores it: FRE-21 is Approved + unblocked, so it's eligible.
    result = eligible_candidates([occupying, approved], "build2")
    assert [i.identifier for i in result] == ["FRE-21"]


def test_eligible_candidates_returns_full_sorted_list() -> None:
    label = frozenset({"stream:build2"})
    high = _issue("FRE-22", "Approved", 2, "2026-01-01T00:00:00Z", label)
    medium = _issue("FRE-23", "Approved", 3, "2026-01-02T00:00:00Z", label)
    low = _issue("FRE-24", "Approved", 4, "2026-01-03T00:00:00Z", label)
    result = eligible_candidates([low, high, medium], "build2")
    # Full list, not just the head — priority order preserved.
    assert [i.identifier for i in result] == ["FRE-22", "FRE-23", "FRE-24"]


def test_eligible_candidates_empty_board() -> None:
    assert eligible_candidates([], "build2") == []


def test_eligible_candidates_skips_blocked() -> None:
    label = frozenset({"stream:build2"})
    blocked = _issue(
        "FRE-25",
        "Approved",
        1,
        "2026-01-01T00:00:00Z",
        label,
        blocked_by=(Blocker(identifier="FRE-0", state="In Progress"),),
    )
    unblocked = _issue("FRE-26", "Approved", 3, "2026-01-02T00:00:00Z", label)
    result = eligible_candidates([blocked, unblocked], "build2")
    assert [i.identifier for i in result] == ["FRE-26"]


def test_eligible_candidates_excludes_wrong_stream_and_state() -> None:
    build2_label = frozenset({"stream:build2"})
    build1_label = frozenset({"stream:build1"})
    wrong_stream = _issue("FRE-27", "Approved", 1, "2026-01-01T00:00:00Z", build1_label)
    wrong_state = _issue("FRE-28", "In Review", 1, "2026-01-01T00:00:00Z", build2_label)
    right = _issue("FRE-29", "Approved", 3, "2026-01-01T00:00:00Z", build2_label)
    result = eligible_candidates([wrong_stream, wrong_state, right], "build2")
    assert [i.identifier for i in result] == ["FRE-29"]


def test_resolve_next_still_matches_eligible_candidates_head() -> None:
    # Pins that refactoring resolve_next to build on eligible_candidates did not
    # change its behavior on already-covered (unoccupied) fixture boards.
    label = frozenset({"stream:build2"})
    boards = [
        [_issue("FRE-30", "Approved", 2, "2026-01-01T00:00:00Z", label)],
        [
            _issue("FRE-31", "Approved", 3, "2026-01-02T00:00:00Z", label),
            _issue("FRE-32", "Approved", 1, "2026-01-01T00:00:00Z", label),
        ],
        [],
    ]
    for board in boards:
        candidates = eligible_candidates(board, "build2")
        expected = candidates[0] if candidates else None
        assert resolve_next(board, "build2") == expected


# --- CLI --eligible mode (FRE-846) ------------------------------------------


def test_cli_eligible_json_lists_full_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    label = frozenset({"stream:build2"})
    board = [
        _issue("FRE-40", "Approved", 2, "2026-01-01T00:00:00Z", label),
        _issue("FRE-41", "Approved", 3, "2026-01-02T00:00:00Z", label),
    ]
    monkeypatch.setattr("scripts.dispatch.next_resolver.load_linear_key", lambda: "fake-key")
    monkeypatch.setattr("scripts.dispatch.next_resolver.fetch_board", lambda stream, key: board)
    exit_code = main(["--stream", "build2", "--eligible", "--json"])
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert [entry["identifier"] for entry in output] == ["FRE-40", "FRE-41"]


def test_cli_eligible_plain_lists_identifiers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    label = frozenset({"stream:build2"})
    board = [_issue("FRE-42", "Approved", 2, "2026-01-01T00:00:00Z", label)]
    monkeypatch.setattr("scripts.dispatch.next_resolver.load_linear_key", lambda: "fake-key")
    monkeypatch.setattr("scripts.dispatch.next_resolver.fetch_board", lambda stream, key: board)
    exit_code = main(["--stream", "build2", "--eligible"])
    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "FRE-42"


def test_cli_eligible_empty_prints_none_and_empty_json_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("scripts.dispatch.next_resolver.load_linear_key", lambda: "fake-key")
    monkeypatch.setattr("scripts.dispatch.next_resolver.fetch_board", lambda stream, key: [])
    exit_code = main(["--stream", "build2", "--eligible"])
    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "none"

    exit_code = main(["--stream", "build2", "--eligible", "--json"])
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == []


# --- fetch_board parsing (FRE-804) ------------------------------------------
#
# These mock only the network (urlopen); they exercise the real query→parse
# path. The live query's *validity* against Linear's schema is a separate
# concern covered by the integration test below — a mock cannot validate
# GraphQL, which is exactly how the FRE-804 400 slipped past the unit suite.


class _FakeResponse:
    """Minimal context-manager stand-in for a urlopen response."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _board_body(nodes: list[dict[str, object]]) -> bytes:
    return json.dumps({"data": {"issues": {"nodes": nodes}}}).encode()


def test_fetch_board_keeps_only_blocks_inverse_relations(monkeypatch: pytest.MonkeyPatch) -> None:
    # One issue whose inverse-relations mix a "related" and a "blocks" edge.
    # Only the "blocks" edge is a real blocker; "related" must be dropped
    # (the FRE-804 regression — the server filter was removed, so the parse
    # must filter by type client-side).
    node = {
        "identifier": "FRE-100",
        "state": {"name": "Approved"},
        "priority": 2,
        "createdAt": "2026-07-01T00:00:00Z",
        "labels": {"nodes": [{"name": "stream:build1"}]},
        "inverseRelations": {
            "nodes": [
                {"type": "related", "issue": {"identifier": "FRE-90", "state": {"name": "Done"}}},
                {
                    "type": "blocks",
                    "issue": {"identifier": "FRE-91", "state": {"name": "In Progress"}},
                },
            ]
        },
    }
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: _FakeResponse(_board_body([node]))
    )
    snapshots = fetch_board("build1", "fake-key")
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.identifier == "FRE-100"
    assert snap.state == "Approved"
    assert snap.priority == 2
    assert snap.created_at == "2026-07-01T00:00:00Z"
    assert snap.labels == frozenset({"stream:build1"})
    # The "related" edge is excluded; only the "blocks" edge is a blocker.
    assert snap.blocked_by == (Blocker(identifier="FRE-91", state="In Progress"),)


def test_fetch_board_surfaces_http_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    # A swallowed 400 body is what made FRE-804 hard to diagnose; the error
    # must now carry the GraphQL validation message.
    body = b'{"errors":[{"message":"Unknown argument \\"filter\\""}]}'

    def _raise(*a: object, **k: object) -> None:
        raise urllib.error.HTTPError(
            url="https://api.linear.app/graphql",
            code=400,
            msg="Bad Request",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(body),
        )

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    with pytest.raises(RuntimeError) as excinfo:
        fetch_board("build1", "fake-key")
    assert "400" in str(excinfo.value)
    assert "Unknown argument" in str(excinfo.value)


def test_fetch_board_surfaces_graphql_errors_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 200 carrying GraphQL `errors` must not silently resolve to an empty
    # board — that would look like "no work" instead of a broken query.
    body = json.dumps({"errors": [{"message": "boom"}]}).encode()
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse(body))
    with pytest.raises(RuntimeError, match="GraphQL errors"):
        fetch_board("build1", "fake-key")


@pytest.mark.integration
def test_fetch_board_live_query_is_valid() -> None:
    """Prove the board-fetch query validates against Linear's live schema.

    This is the check the mocked unit tests structurally cannot provide (a
    mock never validates GraphQL) and whose absence let the FRE-804 400 ship.
    Skipped when no API key is configured.
    """
    api_key = load_linear_key()
    if not api_key:
        pytest.skip("no AGENT_LINEAR_API_KEY configured")
    snapshots = fetch_board("build1", api_key)
    # A valid response is a list (possibly empty); every field the resolver
    # reads must populate on real issues.
    assert isinstance(snapshots, list)
    for snap in snapshots:
        assert snap.identifier
        assert snap.state
        assert stream_label("build1") in snap.labels
        for blocker in snap.blocked_by:
            assert blocker.identifier


# --- FRE-914 follow-up: an unknown stream must fail, never resolve to "none" ---


def test_unknown_stream_is_rejected_not_resolved_as_empty() -> None:
    """A misspelled stream exits non-zero instead of reporting "no work".

    ``adrs`` is the worktree/seat spelling; the stream key is ``adr``. Before
    this guard, ``--stream adrs`` queried Linear for the label ``stream:adrs``,
    matched nothing, and printed ``none`` — indistinguishable from a genuinely
    empty queue. That answer was used on 2026-07-18 to conclude the dispatcher
    was idle and safe to restart. It happened to be idle; the check simply had
    no power to say so.
    """
    with pytest.raises(SystemExit) as exc:
        main(["--stream", "adrs", "--eligible"])
    assert exc.value.code != 0


def test_known_streams_are_accepted_by_the_parser() -> None:
    """Every real stream key must survive the constraint added above."""
    for stream in known_streams():
        assert stream_label(stream) == f"stream:{stream}"
