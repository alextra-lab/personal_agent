# ruff: noqa: D103
"""PR-gate signal-collector tests (ADR-0117, FRE-877).

These pin the **anti-caging contract**: the collector emits raw external facts
only — no derived status, no aggregation, no verdict — reports ``UNKNOWN`` rather
than a silent PASS, exits 0 for every signal value, and reads ``gh`` only (never
Linear/handoff). If a future edit re-grows an opinionated gate, one of these
fails.
"""

from __future__ import annotations

import dataclasses

import pytest
from scripts import pr_gate
from scripts.pr_gate import (
    UNKNOWN,
    collect_signals,
    main,
    parse_author_identity,
    parse_mergeability,
    parse_required_checks,
)

# `gh pr checks --required` text: name<TAB>state<TAB>duration<TAB>url per line.
_GREEN_CHECKS = (
    "Backend unit tests\tpass\t2m24s\thttps://x\nLint (mypy + ruff)\tpass\t52s\thttps://y"
)
_MIXED_CHECKS = (
    "Backend unit tests\tpass\t2m24s\thttps://x\nLint (mypy + ruff)\tfail\t52s\thttps://y"
)
_VIEW_CLEAN = (
    '{"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","isDraft":false,'
    '"author":{"login":"alextra-lab"}}'
)


@dataclasses.dataclass(frozen=True)
class _FakeResult:
    returncode: int
    stdout: str


class _FakeRunner:
    """Maps a gh subcommand (``checks``/``view``) to a canned result; records calls."""

    def __init__(self, *, checks: _FakeResult, view: _FakeResult) -> None:
        self._checks = checks
        self._view = view
        self.calls: list[list[str]] = []

    def __call__(self, argv):  # type: ignore[no-untyped-def]
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if "checks" in joined:
            return self._checks
        if "view" in joined:
            return self._view
        return _FakeResult(1, "")


# --- AC-1: signals accurate (raw, per-field) --------------------------------


def test_green_pr_emits_each_signal_raw() -> None:
    runner = _FakeRunner(checks=_FakeResult(0, _GREEN_CHECKS), view=_FakeResult(0, _VIEW_CLEAN))
    signals = collect_signals(520, runner)
    assert signals["required_checks"] == [
        {"name": "Backend unit tests", "state": "pass"},
        {"name": "Lint (mypy + ruff)", "state": "pass"},
    ]
    assert signals["mergeability"] == {
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "is_draft": False,
    }
    assert signals["author"] == {"is_dependabot_author": False}


def test_red_check_reported_raw_never_aggregated() -> None:
    runner = _FakeRunner(checks=_FakeResult(0, _MIXED_CHECKS), view=_FakeResult(0, _VIEW_CLEAN))
    signals = collect_signals(520, runner)
    states = {c["name"]: c["state"] for c in signals["required_checks"]}
    assert states == {"Backend unit tests": "pass", "Lint (mypy + ruff)": "fail"}
    # No aggregate boolean anywhere — the collector must not decide "CI passed".
    flat = str(signals)
    assert "all_passed" not in flat and "ci_passed" not in flat


# --- AC-2: no judgment, no derived status, exit 0 always --------------------


def test_top_level_keys_are_exactly_the_raw_signals() -> None:
    # The strongest anti-caging assertion: the output can hold ONLY these raw
    # keys — no pass/fail/ready/blocked/warn/hold/merge/recommendation/verdict
    # derived-status field can ever appear (ADR-0117 AC-2).
    runner = _FakeRunner(checks=_FakeResult(0, _GREEN_CHECKS), view=_FakeResult(0, _VIEW_CLEAN))
    signals = collect_signals(520, runner)
    assert set(signals.keys()) == {"pr", "required_checks", "mergeability", "author"}
    forbidden = {
        "pass",
        "fail",
        "ready",
        "blocked",
        "warn",
        "hold",
        "merge",
        "recommendation",
        "verdict",
        "gate",
        "gate_ready",
    }
    assert not (set(signals.keys()) & forbidden)
    assert set(signals["mergeability"].keys()) == {"mergeable", "merge_state_status", "is_draft"}


def test_main_exits_zero_for_red_and_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    # Decision 5: exit 0 for EVERY signal value — never a back-door gate.
    for signals in (
        {
            "pr": 1,
            "required_checks": [{"name": "x", "state": "FAILURE"}],
            "mergeability": {
                "mergeable": "CONFLICTING",
                "merge_state_status": "DIRTY",
                "is_draft": False,
            },
            "author": {"is_dependabot_author": False},
        },
        {
            "pr": 2,
            "required_checks": UNKNOWN,
            "mergeability": {
                "mergeable": UNKNOWN,
                "merge_state_status": UNKNOWN,
                "is_draft": UNKNOWN,
            },
            "author": {"is_dependabot_author": UNKNOWN},
        },
    ):
        monkeypatch.setattr(pr_gate, "collect_signals", lambda _pr, s=signals: s)
        assert main([str(signals["pr"])]) == 0
        assert main([str(signals["pr"]), "--json"]) == 0


# --- AC-3: UNKNOWN is first-class (never a silent PASS) ---------------------


def test_failed_checks_call_is_unknown_not_empty_pass() -> None:
    runner = _FakeRunner(checks=_FakeResult(1, ""), view=_FakeResult(0, _VIEW_CLEAN))
    signals = collect_signals(520, runner)
    assert signals["required_checks"] == UNKNOWN  # not [] — an empty list would read as "all clear"


def test_empty_required_set_is_unknown() -> None:
    assert parse_required_checks("") == UNKNOWN
    assert parse_required_checks("   \n  ") == UNKNOWN


def test_red_or_pending_checks_still_parse_not_unknown() -> None:
    # `gh pr checks` exits non-zero on red/pending, but the per-check states ARE
    # in stdout — the collector reads them, never reporting UNKNOWN for the exact
    # PRs the gate is most useful for (returncode is deliberately ignored).
    parsed = parse_required_checks("Backend unit tests\tfail\t1m\turl\nLint\tpending\t0\turl")
    assert parsed == [
        {"name": "Backend unit tests", "state": "fail"},
        {"name": "Lint", "state": "pending"},
    ]


def test_missing_mergeability_fields_are_unknown() -> None:
    merge = parse_mergeability('{"mergeable":"MERGEABLE"}', 0)
    assert merge["mergeable"] == "MERGEABLE"
    assert merge["merge_state_status"] == UNKNOWN
    assert merge["is_draft"] == UNKNOWN


def test_view_failure_makes_author_unknown() -> None:
    assert parse_author_identity("", 1) == {"is_dependabot_author": UNKNOWN}


def test_dependabot_identity_is_boolean_only() -> None:
    view = '{"author":{"login":"dependabot[bot]"}}'
    assert parse_author_identity(view, 0) == {"is_dependabot_author": True}


# --- AC-5 + Decision 1: gh-only, and "required" delegated to GitHub ---------


def test_collect_calls_gh_only_never_linear() -> None:
    runner = _FakeRunner(checks=_FakeResult(0, _GREEN_CHECKS), view=_FakeResult(0, _VIEW_CLEAN))
    collect_signals(520, runner)
    assert runner.calls, "expected gh calls"
    assert all(call[0] == "gh" for call in runner.calls)


def test_required_flag_delegates_required_set_to_github() -> None:
    # We must not hardcode which checks matter — ask gh for the required set.
    runner = _FakeRunner(checks=_FakeResult(0, _GREEN_CHECKS), view=_FakeResult(0, _VIEW_CLEAN))
    collect_signals(520, runner)
    checks_call = next(c for c in runner.calls if "checks" in " ".join(c))
    assert "--required" in checks_call
