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

import pytest
from scripts.dispatch.next_resolver import Blocker, IssueSnapshot, resolve_next, stream_label


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
