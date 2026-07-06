# ruff: noqa: D103
"""Unit tests for the event-driven gating watcher (FRE-823).

Exercises the pure classification/dedup (`classify_pr`/`decide`) and the
injected-IO tick (`run_once`) against fixtures only — no live gh/Linear/tmux.
The live assembled send-keys into a real session is master-owned verification.

Covers the ticket's acceptance criteria:
  AC-1 master trigger + (pr, head-sha) dedup across ticks
  AC-2 worker bounce trigger + ack-marker dedup
  AC-3 worker CI-red trigger + SHA-keyed ack dedup
  AC-4 no LLM context (import-purity)
  AC-5 injection safety (existing + idle session only)
  AC-6 continuity (never `claude -p`; actuation is `tmux send-keys` only)
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

from scripts.dispatch.gating_watcher import (
    MASTER_SESSION,
    Candidate,
    PullRequest,
    ci_status,
    classify_pr,
    decide,
    fetch_open_prs,
    has_ci_red_ack,
    latest_bounce_unacked,
    parse_ticket_from_branch,
    prune_state,
    run_once,
    send_to_session,
    session_for_labels,
    session_is_idle,
)

_MODULE_PATH = Path("scripts/dispatch/gating_watcher.py")


# --- fakes -----------------------------------------------------------------


class _FakeRunResult:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


class _RecordingRunner:
    """Records argv calls; returns a canned result by first-arg + subcommand."""

    def __init__(self, results: dict[tuple[str, ...], _FakeRunResult] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._results = results or {}

    def __call__(self, argv: Sequence[str]) -> _FakeRunResult:
        argv_t = tuple(argv)
        self.calls.append(argv_t)
        for prefix, result in self._results.items():
            if argv_t[: len(prefix)] == prefix:
                return result
        return _FakeRunResult()


class _NullLogger:
    def info(self, *args: object, **kwargs: object) -> None: ...
    def warning(self, *args: object, **kwargs: object) -> None: ...


def _pr(
    number: int = 412,
    head_ref: str = "fre-823-event-driven-gating-watcher",
    head_sha: str = "abc1234def5678",
    mergeable: str = "MERGEABLE",
    ci: str = "success",
    comment_bodies: tuple[str, ...] = (),
) -> PullRequest:
    return PullRequest(
        number=number,
        head_ref=head_ref,
        head_sha=head_sha,
        mergeable=mergeable,
        ci=ci,  # type: ignore[arg-type]
        comment_bodies=comment_bodies,
    )


_IDLE_PANE = "╭─────╮\n│ >                    │\n╰─────╯\n  ? for shortcuts"
_BUSY_PANE = "✽ Working… (esc to interrupt)"


# --- parse_ticket_from_branch ----------------------------------------------


def test_parse_ticket_from_branch() -> None:
    assert parse_ticket_from_branch("fre-823-event-driven-gating-watcher") == "FRE-823"
    assert parse_ticket_from_branch("FRE-9-foo") == "FRE-9"
    assert parse_ticket_from_branch("main") is None
    assert parse_ticket_from_branch("feature/x") is None


# --- ci_status -------------------------------------------------------------


def test_ci_status_empty_is_pending() -> None:
    assert ci_status([]) == "pending"


def test_ci_status_all_success() -> None:
    rollup = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SKIPPED"},
        {"__typename": "StatusContext", "state": "SUCCESS"},
    ]
    assert ci_status(rollup) == "success"


def test_ci_status_any_failure() -> None:
    rollup = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
    ]
    assert ci_status(rollup) == "failure"


def test_ci_status_incomplete_is_pending() -> None:
    rollup = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None},
    ]
    assert ci_status(rollup) == "pending"


def test_ci_status_unknown_conclusion_is_pending_not_failure() -> None:
    rollup = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": ""}]
    assert ci_status(rollup) == "pending"


# --- latest_bounce_unacked -------------------------------------------------


def test_bounce_unacked_when_no_ack() -> None:
    assert latest_bounce_unacked(("## Master gate — BOUNCE\nfix X",)) is True


def test_bounce_acked_when_ack_after() -> None:
    bodies = ("## Master gate — BOUNCE", "Ack: addressing master bounce in next push.")
    assert latest_bounce_unacked(bodies) is False


def test_bounce_unacked_when_newer_bounce_after_ack() -> None:
    bodies = (
        "## Master gate — BOUNCE",
        "Ack: addressing master bounce",
        "## Master gate — BOUNCE second round",
    )
    assert latest_bounce_unacked(bodies) is True


def test_no_bounce_is_not_unacked() -> None:
    assert latest_bounce_unacked(("looks good",)) is False


# --- has_ci_red_ack --------------------------------------------------------


def test_ci_red_ack_prefix_match() -> None:
    bodies = ("Ack: addressing red CI at abc1234 in next push.",)
    assert has_ci_red_ack(bodies, "abc1234def5678") is True


def test_ci_red_ack_mismatched_sha() -> None:
    bodies = ("Ack: addressing red CI at deadbee in next push.",)
    assert has_ci_red_ack(bodies, "abc1234def5678") is False


def test_ci_red_ack_absent() -> None:
    assert has_ci_red_ack(("nope",), "abc1234def5678") is False


# --- session_is_idle -------------------------------------------------------


def test_session_idle_true_at_prompt() -> None:
    assert session_is_idle(_IDLE_PANE) is True


def test_session_idle_false_when_busy() -> None:
    assert session_is_idle(_BUSY_PANE) is False


def test_session_idle_false_on_permission_prompt() -> None:
    pane = "Do you want to proceed?\n❯ 1. Yes\n  2. No, and tell Claude"
    assert session_is_idle(pane) is False


def test_session_idle_false_on_blank_or_shell() -> None:
    assert session_is_idle("debian@vps:/opt/seshat$ ") is False


# --- session_for_labels ----------------------------------------------------


def test_session_for_labels_maps_stream() -> None:
    assert session_for_labels({"stream:build2", "Tier-1:Opus"}) == "cc-build2"
    assert session_for_labels({"stream:build1"}) == "cc-build"
    assert session_for_labels({"stream:adr"}) == "cc-adrs"


def test_session_for_labels_none_without_stream() -> None:
    assert session_for_labels({"Tier-1:Opus", "PersonalAgent"}) is None


# --- classify_pr / decide: AC-1 master -------------------------------------


def _no_session(_ticket: str | None) -> str | None:
    return None


def test_master_ready_classifies_master() -> None:
    cand = classify_pr(_pr(), now=100.0, sent={}, master_ttl_s=600, worker_ttl_s=60)
    assert cand == Candidate("master", "master-ready", "master:412:abc1234def5678", 600)


def test_master_decide_routes_to_cc_master() -> None:
    triggers = decide(
        [_pr()], session_resolver=_no_session, now=100.0, sent={}, master_ttl_s=600, worker_ttl_s=60
    )
    assert len(triggers) == 1
    assert triggers[0].session == MASTER_SESSION
    assert triggers[0].command == "/master 412"


def test_master_conflicting_does_not_trigger() -> None:
    cand = classify_pr(
        _pr(mergeable="CONFLICTING"), now=100.0, sent={}, master_ttl_s=600, worker_ttl_s=60
    )
    assert cand is None


def test_master_unknown_mergeable_still_triggers() -> None:
    cand = classify_pr(
        _pr(mergeable="UNKNOWN"), now=100.0, sent={}, master_ttl_s=600, worker_ttl_s=60
    )
    assert cand is not None and cand.kind == "master"


def test_master_pending_ci_does_not_trigger() -> None:
    assert (
        classify_pr(_pr(ci="pending"), now=100.0, sent={}, master_ttl_s=600, worker_ttl_s=60)
        is None
    )


def test_master_dedup_same_sha_suppressed() -> None:
    sent = {"master:412:abc1234def5678": 100.0}
    assert classify_pr(_pr(), now=200.0, sent=sent, master_ttl_s=600, worker_ttl_s=60) is None


def test_master_dedup_re_arms_after_ttl() -> None:
    sent = {"master:412:abc1234def5678": 100.0}
    cand = classify_pr(_pr(), now=800.0, sent=sent, master_ttl_s=600, worker_ttl_s=60)
    assert cand is not None and cand.kind == "master"


def test_master_new_sha_triggers_again() -> None:
    sent = {"master:412:abc1234def5678": 100.0}
    cand = classify_pr(
        _pr(head_sha="ff99newsha"), now=150.0, sent=sent, master_ttl_s=600, worker_ttl_s=60
    )
    assert cand is not None and cand.key == "master:412:ff99newsha"


# --- classify_pr / decide: AC-2 worker bounce ------------------------------


def test_worker_bounce_classifies_worker() -> None:
    pr = _pr(comment_bodies=("## Master gate — BOUNCE\nfix",))
    cand = classify_pr(pr, now=100.0, sent={}, master_ttl_s=600, worker_ttl_s=60)
    assert cand == Candidate("worker", "worker-bounce", "worker:412:abc1234def5678", 60)


def test_worker_bounce_routes_to_stream_session() -> None:
    pr = _pr(comment_bodies=("## Master gate — BOUNCE",))
    triggers = decide(
        [pr],
        session_resolver=lambda t: "cc-build2" if t == "FRE-823" else None,
        now=100.0,
        sent={},
        master_ttl_s=600,
        worker_ttl_s=60,
    )
    assert triggers[0].session == "cc-build2"
    assert triggers[0].command == "/prime-worker"


def test_worker_bounce_acked_does_not_trigger() -> None:
    # ci=pending isolates the ack: no worker trigger, and not yet master-ready.
    pr = _pr(
        ci="pending", comment_bodies=("## Master gate — BOUNCE", "Ack: addressing master bounce")
    )
    assert classify_pr(pr, now=100.0, sent={}, master_ttl_s=600, worker_ttl_s=60) is None


def test_worker_bounce_acked_and_green_re_triggers_master() -> None:
    # An acked bounce that is now green is master-ready again (re-review the fix).
    pr = _pr(
        ci="success", comment_bodies=("## Master gate — BOUNCE", "Ack: addressing master bounce")
    )
    cand = classify_pr(pr, now=100.0, sent={}, master_ttl_s=600, worker_ttl_s=60)
    assert cand is not None and cand.kind == "master"


def test_worker_bounce_lease_suppresses_pre_ack_resend() -> None:
    pr = _pr(comment_bodies=("## Master gate — BOUNCE",))
    sent = {"worker:412:abc1234def5678": 100.0}
    # within the lease window → suppressed even though the ack has not landed yet
    assert classify_pr(pr, now=130.0, sent=sent, master_ttl_s=600, worker_ttl_s=60) is None


# --- classify_pr: AC-3 worker CI-red ---------------------------------------


def test_worker_ci_red_triggers() -> None:
    pr = _pr(ci="failure")
    cand = classify_pr(pr, now=100.0, sent={}, master_ttl_s=600, worker_ttl_s=60)
    assert cand is not None and cand.reason == "worker-ci-red"


def test_worker_ci_red_acked_for_sha_does_not_trigger() -> None:
    pr = _pr(ci="failure", comment_bodies=("Ack: addressing red CI at abc1234 in next push.",))
    assert classify_pr(pr, now=100.0, sent={}, master_ttl_s=600, worker_ttl_s=60) is None


def test_worker_ci_red_ack_for_old_sha_still_triggers_new_sha() -> None:
    pr = _pr(
        ci="failure", head_sha="newsha99", comment_bodies=("Ack: addressing red CI at abc1234",)
    )
    cand = classify_pr(pr, now=100.0, sent={}, master_ttl_s=600, worker_ttl_s=60)
    assert cand is not None and cand.reason == "worker-ci-red"


def test_worker_bounce_takes_precedence_over_ci_red() -> None:
    pr = _pr(ci="failure", comment_bodies=("## Master gate — BOUNCE",))
    cand = classify_pr(pr, now=100.0, sent={}, master_ttl_s=600, worker_ttl_s=60)
    assert cand is not None and cand.reason == "worker-bounce"


# --- prune_state -----------------------------------------------------------


def test_prune_drops_closed_pr_and_expired() -> None:
    sent = {
        "master:412:aaa": 100.0,  # kept: open + fresh
        "master:999:bbb": 100.0,  # dropped: PR not open
        "worker:412:ccc": 10.0,  # dropped: expired
    }
    kept = prune_state(sent, now=200.0, max_ttl_s=150.0, open_prs=[412])
    assert kept == {"master:412:aaa": 100.0}


# --- send_to_session: AC-5 injection safety --------------------------------


def test_send_absent_session_skips() -> None:
    runner = _RecordingRunner({("tmux", "has-session"): _FakeRunResult(returncode=1)})
    assert send_to_session("cc-master", "/master 1", runner) == "absent"
    assert not any(call[:2] == ("tmux", "send-keys") for call in runner.calls)


def test_send_busy_session_skips() -> None:
    runner = _RecordingRunner(
        {
            ("tmux", "has-session"): _FakeRunResult(returncode=0),
            ("tmux", "capture-pane"): _FakeRunResult(returncode=0, stdout=_BUSY_PANE),
        }
    )
    assert send_to_session("cc-master", "/master 1", runner) == "busy"
    assert not any(call[:2] == ("tmux", "send-keys") for call in runner.calls)


def test_send_idle_session_injects() -> None:
    runner = _RecordingRunner(
        {
            ("tmux", "has-session"): _FakeRunResult(returncode=0),
            ("tmux", "capture-pane"): _FakeRunResult(returncode=0, stdout=_IDLE_PANE),
        }
    )
    assert send_to_session("cc-master", "/master 1", runner) == "sent"
    send_keys = [call for call in runner.calls if call[:2] == ("tmux", "send-keys")]
    assert send_keys == [
        ("tmux", "send-keys", "-t", "cc-master", "-l", "/master 1"),
        ("tmux", "send-keys", "-t", "cc-master", "Enter"),
    ]


# --- run_once: AC-1 dedup across ticks, AC-6 continuity ---------------------


def _idle_runner() -> _RecordingRunner:
    return _RecordingRunner(
        {
            ("tmux", "has-session"): _FakeRunResult(returncode=0),
            ("tmux", "capture-pane"): _FakeRunResult(returncode=0, stdout=_IDLE_PANE),
        }
    )


def test_run_once_master_dedup_across_ticks() -> None:
    pr = _pr()
    saved: dict[str, float] = {}

    def persist(state: dict[str, float]) -> None:
        saved.clear()
        saved.update(state)

    runner = _idle_runner()
    state: dict[str, float] = {}
    # tick 1 — sends /master, records the key
    run_once(
        state,
        now=100.0,
        board_fetcher=lambda: [pr],
        session_resolver=_no_session,
        runner=runner,
        persist=persist,
        logger=_NullLogger(),
        execute=True,
    )
    sends_tick1 = [c for c in runner.calls if c[:2] == ("tmux", "send-keys")]
    assert ("tmux", "send-keys", "-t", "cc-master", "-l", "/master 412") in sends_tick1
    assert saved == {"master:412:abc1234def5678": 100.0}

    # tick 2 — same PR/sha, within TTL → no re-send
    runner2 = _idle_runner()
    run_once(
        dict(saved),
        now=150.0,
        board_fetcher=lambda: [pr],
        session_resolver=_no_session,
        runner=runner2,
        persist=persist,
        logger=_NullLogger(),
        execute=True,
    )
    assert not any(c[:2] == ("tmux", "send-keys") for c in runner2.calls)


def test_run_once_uses_only_tmux_and_gh_never_claude() -> None:
    pr = _pr(comment_bodies=("## Master gate — BOUNCE",))
    runner = _idle_runner()
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [pr],
        session_resolver=lambda _t: "cc-build2",
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
    )
    # AC-6: the watcher never launches a Claude session; actuation is tmux only.
    assert all(call[0] != "claude" for call in runner.calls)
    assert any(call[:2] == ("tmux", "send-keys") for call in runner.calls)


def test_run_once_kill_switch_halts_actuation() -> None:
    runner = _idle_runner()
    fetched = []
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: fetched.append(1) or [_pr()],  # type: ignore[func-returns-value]
        session_resolver=_no_session,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        kill_switch_engaged=lambda: True,
    )
    assert runner.calls == []  # nothing shelled, board not even fetched
    assert fetched == []


def test_run_once_unroutable_worker_skips_without_send() -> None:
    pr = _pr(comment_bodies=("## Master gate — BOUNCE",))
    runner = _idle_runner()
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [pr],
        session_resolver=_no_session,  # no stream label → unroutable
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
    )
    assert not any(c[:2] == ("tmux", "send-keys") for c in runner.calls)


def test_run_once_dry_run_sends_nothing() -> None:
    runner = _idle_runner()
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [_pr()],
        session_resolver=_no_session,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=False,
    )
    assert not any(c[:2] == ("tmux", "send-keys") for c in runner.calls)


# --- fetch_open_prs (gh parsing) -------------------------------------------


def test_fetch_open_prs_parses_view_snapshot() -> None:
    list_json = '[{"number": 412}]'
    view_json = (
        '{"number": 412, "headRefName": "fre-823-x", "headRefOid": "abc123",'
        ' "mergeable": "MERGEABLE",'
        ' "statusCheckRollup": [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}],'
        ' "comments": [{"body": "hi", "createdAt": "2026-07-06T00:00:00Z"}]}'
    )
    runner = _RecordingRunner(
        {
            ("gh", "pr", "list"): _FakeRunResult(returncode=0, stdout=list_json),
            ("gh", "pr", "view"): _FakeRunResult(returncode=0, stdout=view_json),
        }
    )
    prs = fetch_open_prs(runner)
    assert len(prs) == 1
    assert prs[0].number == 412
    assert prs[0].head_ref == "fre-823-x"
    assert prs[0].ci == "success"
    assert prs[0].comment_bodies == ("hi",)


# --- AC-4: import purity ---------------------------------------------------


def test_module_imports_no_llm_client() -> None:
    """AC-4: the watcher holds no model context — it imports no LLM client."""
    tree = ast.parse(_MODULE_PATH.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {
        "litellm",
        "anthropic",
        "openai",
        "personal_agent.llm_client",
        "personal_agent.orchestrator",
        "dspy",
    }
    offending = {
        name for name in imported for bad in forbidden if name == bad or name.startswith(bad + ".")
    }
    assert offending == set(), f"watcher must import no LLM client, found: {offending}"
