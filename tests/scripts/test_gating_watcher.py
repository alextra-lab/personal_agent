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

Also covers FRE-829's wiring of the durable trigger ledger into `run_once`
(the ledger's own crash/dedup semantics are unit-tested independently in
`test_trigger_ledger.py`) — this file only proves the *integration points*:
where a ledger entry is written, abandoned, or left untouched.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from collections.abc import Sequence
from pathlib import Path

from scripts.dispatch import launcher
from scripts.dispatch.gating_watcher import (
    DEFAULT_CONTEXT_PRESSURE_THRESHOLD,
    MASTER_SESSION,
    Candidate,
    CheckResult,
    ContextReading,
    PullRequest,
    _context_pressure_threshold_default,
    build_channel_payload,
    ci_status,
    classify_pr,
    context_pressure,
    decide,
    fetch_open_prs,
    has_ci_red_ack,
    load_channel_secret,
    parse_ticket_from_branch,
    post_channel_event,
    prune_state,
    run_once,
    send_to_session,
    session_for_labels,
    session_is_idle,
)
from scripts.dispatch.tmux_target import exact_pane
from scripts.dispatch.trigger_ledger import snapshot_unconsumed

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
    checks: tuple[CheckResult, ...] = (),
    is_dependabot: bool = False,
) -> PullRequest:
    return PullRequest(
        number=number,
        head_ref=head_ref,
        head_sha=head_sha,
        mergeable=mergeable,
        ci=ci,  # type: ignore[arg-type]
        comment_bodies=comment_bodies,
        checks=checks,
        is_dependabot=is_dependabot,
    )


_FIXTURES_DIR = Path("tests/fixtures")
# Real ``tmux capture-pane -p`` output (FRE-825: the prior synthetic ``│ >``
# fixture never matched any live pane — this is the actual rendering).
_REAL_IDLE_PANE = (_FIXTURES_DIR / "gating_watcher_real_idle_pane.txt").read_text(encoding="utf-8")
_REAL_BUSY_SPINNER_PANE = (_FIXTURES_DIR / "gating_watcher_real_busy_pane.txt").read_text(
    encoding="utf-8"
)
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


def test_session_idle_true_at_real_captured_prompt() -> None:
    # FRE-825: real ``tmux capture-pane`` output, not synthetic ``│ >`` text.
    assert session_is_idle(_REAL_IDLE_PANE) is True


def test_session_idle_false_when_busy() -> None:
    assert session_is_idle(_BUSY_PANE) is False


def test_session_idle_false_on_real_captured_busy_spinner() -> None:
    # FRE-825: a real captured in-progress pane (``<verb>ing… (Nm Ns · ...)``)
    # — the caret box renders even mid-turn, so the spinner is the only signal.
    # This fixture predates ``esc to interrupt``/``Running…`` appearing at all,
    # so it regression-tests ``_BUSY_SPINNER_RE`` specifically.
    assert session_is_idle(_REAL_BUSY_SPINNER_PANE) is False


def test_session_idle_false_on_permission_prompt() -> None:
    pane = "Do you want to proceed?\n❯ 1. Yes\n  2. No, and tell Claude"
    assert session_is_idle(pane) is False


def test_session_idle_false_on_blank_or_shell() -> None:
    assert session_is_idle("debian@vps:/opt/seshat$ ") is False


def test_session_idle_true_with_ellipsis_paren_prose_not_on_status_line() -> None:
    # _BUSY_SPINNER_RE is anchored to a ``●``-prefixed status line so ordinary
    # transcript prose containing "word… (" does not false-positive as busy.
    pane = "Retrying the request… (will give up after 3 attempts)\n❯\xa0"
    assert session_is_idle(pane) is True


def test_session_idle_true_when_marker_words_appear_only_in_scrollback_prose() -> None:
    # FRE-845: a completed turn's own response prose routinely contains
    # phrasing that overlaps a busy-marker word ("Do you want...?", a
    # numbered list, "Running the tests…", "Compacting the summary…"). The
    # busy-marker substring check must be scoped to the pane's active/live
    # region near the input box, not the whole scrollback -- otherwise a
    # long-idle master with such prose still visible above the box is
    # chronically mis-flagged busy (the live incident: gating_skip
    # reason=busy every tick for ~3 hours with a master sitting idle).
    prose = (
        "Do you want me to proceed with the migration? Here is the plan:\n"
        "1. Yes, run the migration script now.\n"
        "2. No, and tell Claude to hold off until review.\n"
        "The test suite finished Running… (all green)\n"
        "Compacting the summary below for readability.\n"
    )
    padding = "\n".join(f"filler transcript line {i}" for i in range(40))
    pane = prose + "\n" + padding + "\n" + _REAL_IDLE_PANE
    assert session_is_idle(pane) is True


def test_session_idle_false_on_tall_permission_prompt_near_bottom() -> None:
    # A multi-option decision prompt can push its "Do you want"/"1. Yes" text
    # a little higher than a single-line prompt -- still within the active
    # region near the bottom of the pane, so it must still read busy.
    prompt = (
        "Do you want to proceed with this multi-file refactor?\n"
        "❯ 1. Yes\n"
        "  2. Yes, and don't ask again for file edits\n"
        "  3. No, and tell Claude what to do differently\n"
    )
    pane = "some earlier transcript line\n" * 10 + prompt
    assert session_is_idle(pane) is False


# --- session_for_labels ----------------------------------------------------


def test_session_for_labels_maps_stream() -> None:
    assert session_for_labels({"stream:build2", "Tier-1:Opus"}) == "cc-2build"
    assert session_for_labels({"stream:build1"}) == "cc-1build"
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


# --- classify_pr / decide: worker CI-red -----------------------------------
# Bounce is master-direct now (master send-keys the worker itself), so a red CI
# on the head SHA is the only watcher-owned worker trigger.


def test_worker_ci_red_routes_to_stream_session_with_message() -> None:
    pr = _pr(ci="failure")
    triggers = decide(
        [pr],
        session_resolver=lambda t: "cc-2build" if t == "FRE-823" else None,
        now=100.0,
        sent={},
        master_ttl_s=600,
        worker_ttl_s=60,
    )
    assert triggers[0].session == "cc-2build"
    assert triggers[0].command == "PR #412 failed CI checks - correct them"


def test_decide_worker_trigger_send_keys_mode_for_non_cutover_seat(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # All three live worker seats are channel-mode now (FRE-875 Phase B), so the
    # send_keys delivery path — still the dataclass default and the channel-down
    # fallback — is constructed explicitly here.
    monkeypatch.setitem(
        launcher._TOPOLOGY,
        "build2",
        dataclasses.replace(launcher._TOPOLOGY["build2"], mode="send_keys"),
    )
    pr = _pr(ci="failure")
    triggers = decide(
        [pr],
        session_resolver=lambda t: "cc-2build" if t == "FRE-823" else None,
        now=100.0,
        sent={},
        master_ttl_s=600,
        worker_ttl_s=60,
    )
    assert triggers[0].mode == "send_keys"
    assert triggers[0].channel_port is None
    assert triggers[0].channel_payload is None


def test_decide_worker_trigger_channel_mode_carries_port_and_payload(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setitem(
        launcher._TOPOLOGY,
        "build2",
        dataclasses.replace(launcher._TOPOLOGY["build2"], mode="channel"),
    )
    pr = _pr(ci="failure", checks=(CheckResult("pytest", "fail", "FAILURE", "https://ci/1"),))
    triggers = decide(
        [pr],
        session_resolver=lambda t: "cc-2build" if t == "FRE-823" else None,
        now=100.0,
        sent={},
        master_ttl_s=600,
        worker_ttl_s=60,
    )
    assert triggers[0].mode == "channel"
    assert triggers[0].channel_port == launcher.topology_for("build2").channel_port
    assert triggers[0].channel_payload == build_channel_payload(pr)


def test_decide_master_trigger_always_send_keys_mode(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Master has no StreamTopology entry -- unaffected even if a worker stream
    # is cut over to channel mode.
    monkeypatch.setitem(
        launcher._TOPOLOGY,
        "build2",
        dataclasses.replace(launcher._TOPOLOGY["build2"], mode="channel"),
    )
    triggers = decide(
        [_pr()], session_resolver=_no_session, now=100.0, sent={}, master_ttl_s=600, worker_ttl_s=60
    )
    assert triggers[0].mode == "send_keys"
    assert triggers[0].channel_port is None
    assert triggers[0].channel_payload is None


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


# --- classify_pr: dependabot boundary guard (FRE-872, ADR-0116 AC-6) --------
# A dependabot-authored PR is NEVER a worker candidate -- structural
# defense-in-depth (the gateway can never hand a seat an instruction whose
# natural completion is "push to the dependabot branch"). Scoped to the
# worker path only: the master-ready path is a pure, harmless notification
# (master's own gate re-reads live state and decides), so a CI-green,
# mergeable dependabot PR still surfaces to master exactly like any other PR
# -- suppressing it too was a code-review-caught regression (removed a
# pre-existing capability with no replacement notification path).


def test_classify_pr_dependabot_ci_red_never_routes_to_worker() -> None:
    pr = _pr(ci="failure", is_dependabot=True)
    assert classify_pr(pr, now=100.0, sent={}, master_ttl_s=600, worker_ttl_s=60) is None


def test_classify_pr_dependabot_ci_green_still_routes_to_master() -> None:
    # The watcher is master's sole automated detector -- suppressing this
    # notification would leave a green dependabot PR unnoticed indefinitely.
    pr = _pr(ci="success", mergeable="MERGEABLE", is_dependabot=True)
    cand = classify_pr(pr, now=100.0, sent={}, master_ttl_s=600, worker_ttl_s=60)
    assert cand is not None and cand.kind == "master"


def test_classify_pr_non_dependabot_unaffected_by_guard() -> None:
    # Regression: the guard must not accidentally suppress an ordinary PR.
    pr = _pr(ci="failure", is_dependabot=False)
    cand = classify_pr(pr, now=100.0, sent={}, master_ttl_s=600, worker_ttl_s=60)
    assert cand is not None and cand.kind == "worker"


# --- build_channel_payload (FRE-872, ADR-0116 AC-3 code-layer proof) -------


def test_build_channel_payload_reflects_pr_identity_and_checks() -> None:
    pr = _pr(
        number=412,
        head_sha="abc1234def5678",
        head_ref="fre-823-x",
        mergeable="MERGEABLE",
        checks=(
            CheckResult(
                name="pytest", state="fail", conclusion="FAILURE", details_url="https://ci/1"
            ),
        ),
        is_dependabot=False,
    )
    payload = build_channel_payload(pr)
    assert payload["pr"] == 412
    assert payload["head_sha"] == "abc1234def5678"
    assert payload["head_ref"] == "fre-823-x"
    assert payload["mergeable"] == "MERGEABLE"
    assert payload["dependabot"] is False
    assert payload["checks"] == [
        {"name": "pytest", "state": "fail", "conclusion": "FAILURE", "details_url": "https://ci/1"}
    ]


def test_build_channel_payload_distinguishes_different_fixtures() -> None:
    # AC-3's code-layer proof: distinct inputs must produce distinguishably
    # different payloads, so a downstream reader (the seat) can act on the
    # exact delivered state, not a canned response.
    pr_a = _pr(
        head_sha="aaa111",
        checks=(CheckResult("mypy", "fail", "FAILURE", "https://ci/a"),),
        is_dependabot=False,
    )
    pr_b = _pr(
        head_sha="bbb222",
        checks=(CheckResult("ruff", "fail", "FAILURE", "https://ci/b"),),
        is_dependabot=True,
    )
    payload_a = build_channel_payload(pr_a)
    payload_b = build_channel_payload(pr_b)
    assert payload_a != payload_b
    assert payload_a["head_sha"] != payload_b["head_sha"]
    assert payload_a["checks"] != payload_b["checks"]
    assert payload_a["dependabot"] != payload_b["dependabot"]


def test_build_channel_payload_json_serializable() -> None:
    payload = build_channel_payload(_pr(checks=(CheckResult("pytest", "pass", "SUCCESS", ""),)))
    json.dumps(payload)  # must not raise


# --- fetch_open_prs: author/checks parsing (FRE-872) ------------------------


def test_fetch_open_prs_detects_dependabot_author() -> None:
    list_json = '[{"number": 500}]'
    view_json = (
        '{"number": 500, "headRefName": "dependabot/npm_and_yarn/x", "headRefOid": "def456",'
        ' "mergeable": "MERGEABLE", "author": {"login": "dependabot[bot]"},'
        ' "statusCheckRollup": [], "comments": []}'
    )
    runner = _RecordingRunner(
        {
            ("gh", "pr", "list"): _FakeRunResult(returncode=0, stdout=list_json),
            ("gh", "pr", "view"): _FakeRunResult(returncode=0, stdout=view_json),
        }
    )
    prs = fetch_open_prs(runner)
    assert prs[0].is_dependabot is True


def test_fetch_open_prs_non_dependabot_author_is_false() -> None:
    list_json = '[{"number": 412}]'
    view_json = (
        '{"number": 412, "headRefName": "fre-823-x", "headRefOid": "abc123",'
        ' "mergeable": "MERGEABLE", "author": {"login": "lextra"},'
        ' "statusCheckRollup": [], "comments": []}'
    )
    runner = _RecordingRunner(
        {
            ("gh", "pr", "list"): _FakeRunResult(returncode=0, stdout=list_json),
            ("gh", "pr", "view"): _FakeRunResult(returncode=0, stdout=view_json),
        }
    )
    prs = fetch_open_prs(runner)
    assert prs[0].is_dependabot is False


def test_fetch_open_prs_missing_author_defaults_false() -> None:
    list_json = '[{"number": 412}]'
    view_json = (
        '{"number": 412, "headRefName": "fre-823-x", "headRefOid": "abc123",'
        ' "mergeable": "MERGEABLE", "statusCheckRollup": [], "comments": []}'
    )
    runner = _RecordingRunner(
        {
            ("gh", "pr", "list"): _FakeRunResult(returncode=0, stdout=list_json),
            ("gh", "pr", "view"): _FakeRunResult(returncode=0, stdout=view_json),
        }
    )
    prs = fetch_open_prs(runner)
    assert prs[0].is_dependabot is False


def test_fetch_open_prs_builds_checks_tuple_from_rollup() -> None:
    list_json = '[{"number": 412}]'
    view_json = (
        '{"number": 412, "headRefName": "fre-823-x", "headRefOid": "abc123",'
        ' "mergeable": "MERGEABLE", "author": {"login": "lextra"},'
        ' "statusCheckRollup": [{"__typename": "CheckRun", "name": "pytest",'
        ' "status": "COMPLETED", "conclusion": "FAILURE", "detailsUrl": "https://ci/1"}],'
        ' "comments": []}'
    )
    runner = _RecordingRunner(
        {
            ("gh", "pr", "list"): _FakeRunResult(returncode=0, stdout=list_json),
            ("gh", "pr", "view"): _FakeRunResult(returncode=0, stdout=view_json),
        }
    )
    prs = fetch_open_prs(runner)
    assert prs[0].checks == (
        CheckResult(name="pytest", state="fail", conclusion="FAILURE", details_url="https://ci/1"),
    )


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


def test_send_master_injects_regardless_of_busy_pane() -> None:
    """Master triggers (require_idle=False) send even into a busy pane and never
    consult capture-pane for an idle check — Claude Code queues the keys.
    """
    runner = _RecordingRunner(
        {
            ("tmux", "has-session"): _FakeRunResult(returncode=0),
            ("tmux", "capture-pane"): _FakeRunResult(returncode=0, stdout=_BUSY_PANE),
        }
    )
    assert send_to_session("cc-master", "/master 1", runner, require_idle=False) == "sent"
    assert not any(call[:2] == ("tmux", "capture-pane") for call in runner.calls)
    send_keys = [call for call in runner.calls if call[:2] == ("tmux", "send-keys")]
    assert send_keys == [
        ("tmux", "send-keys", "-t", "=cc-master:0.0", "-l", "/master 1"),
        ("tmux", "send-keys", "-t", "=cc-master:0.0", "Enter"),
    ]


def test_send_idle_session_injects() -> None:
    runner = _RecordingRunner(
        {
            ("tmux", "has-session"): _FakeRunResult(returncode=0),
            ("tmux", "capture-pane"): _FakeRunResult(returncode=0, stdout=_REAL_IDLE_PANE),
        }
    )
    assert send_to_session("cc-master", "/master 1", runner) == "sent"
    send_keys = [call for call in runner.calls if call[:2] == ("tmux", "send-keys")]
    assert send_keys == [
        ("tmux", "send-keys", "-t", "=cc-master:0.0", "-l", "/master 1"),
        ("tmux", "send-keys", "-t", "=cc-master:0.0", "Enter"),
    ]


# --- run_once: AC-1 dedup across ticks, AC-6 continuity ---------------------


def _idle_runner() -> _RecordingRunner:
    return _RecordingRunner(
        {
            ("tmux", "has-session"): _FakeRunResult(returncode=0),
            ("tmux", "capture-pane"): _FakeRunResult(returncode=0, stdout=_REAL_IDLE_PANE),
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
    assert ("tmux", "send-keys", "-t", "=cc-master:0.0", "-l", "/master 412") in sends_tick1
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
    pr = _pr(ci="failure")
    runner = _idle_runner()
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [pr],
        session_resolver=lambda _t: "cc-2build",
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
    pr = _pr(ci="failure")
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


# --- FRE-829: trigger ledger wiring -----------------------------------------


def test_run_once_ledger_records_and_consumes_on_successful_send() -> None:
    ledger: dict = {}
    runner = _idle_runner()
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [_pr()],
        session_resolver=_no_session,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        ledger=ledger,
        ledger_persist=ledger.update,
    )
    unconsumed = snapshot_unconsumed(ledger)
    assert unconsumed == ()  # fully closed out
    entry = ledger["master:412:abc1234def5678"]
    assert entry.sent_at is not None
    assert entry.consumed_at is not None


def test_run_once_ledger_untouched_for_idle_pr() -> None:
    ledger: dict = {}
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [_pr(ci="pending")],
        session_resolver=_no_session,
        runner=_idle_runner(),
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        ledger=ledger,
        ledger_persist=ledger.update,
    )
    assert ledger == {}


def test_run_once_ledger_untouched_in_dry_run() -> None:
    ledger: dict = {}
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [_pr()],
        session_resolver=_no_session,
        runner=_idle_runner(),
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=False,
        ledger=ledger,
        ledger_persist=ledger.update,
    )
    assert ledger == {}


def test_run_once_ledger_untouched_for_unroutable_worker() -> None:
    pr = _pr(ci="failure")
    ledger: dict = {}
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [pr],
        session_resolver=_no_session,  # unroutable
        runner=_idle_runner(),
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        ledger=ledger,
        ledger_persist=ledger.update,
    )
    assert ledger == {}


def test_run_once_master_sends_even_on_busy_session() -> None:
    # Master triggers bypass the idle guard (require_idle=False): the send goes
    # through even into a busy pane, and Claude Code queues it. (Workers still
    # abandon-on-busy and retry — send_to_session's require_idle default.)
    runner = _RecordingRunner(
        {
            ("tmux", "has-session"): _FakeRunResult(returncode=0),
            ("tmux", "capture-pane"): _FakeRunResult(returncode=0, stdout=_BUSY_PANE),
        }
    )
    ledger: dict = {}
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [_pr()],
        session_resolver=_no_session,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        ledger=ledger,
        ledger_persist=ledger.update,
    )
    entry = ledger["master:412:abc1234def5678"]
    assert entry.sent_at is not None  # sent despite the busy pane
    assert entry.consumed_at is not None


def test_run_once_reconciles_pending_ledger_entry_before_new_decisions() -> None:
    # Seed a ledger entry representing a crash right after ledger-write (no
    # send ever attempted) -- run_once must reconcile it (complete-exactly-once)
    # before evaluating any new board state, even when the board has no
    # matching PR this tick.
    from scripts.dispatch.trigger_ledger import record_pending

    seeded, _ = record_pending(
        {},
        event_id="master:999:deadbeef",
        source="master-ready",
        target_pane="cc-master",
        ticket="999",
        command="/master 999",
        preconditions={"head_sha": "deadbeef"},
        now=50.0,
        ttl_s=600.0,
    )
    runner = _idle_runner()
    ledger = dict(seeded)
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [],  # nothing new this tick
        session_resolver=_no_session,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        ledger=ledger,
        ledger_persist=ledger.update,
    )
    sends = [c for c in runner.calls if c[:2] == ("tmux", "send-keys")]
    assert ("tmux", "send-keys", "-t", "=cc-master:0.0", "-l", "/master 999") in sends
    assert ledger["master:999:deadbeef"].consumed_at is not None


# --- FRE-872, ADR-0116: channel-mode delivery + fallback --------------------


def _channel_worker_pr() -> PullRequest:
    return _pr(ci="failure", checks=(CheckResult("pytest", "fail", "FAILURE", "https://ci/1"),))


def _resolve_build2(ticket: str | None) -> str | None:
    return "cc-2build" if ticket == "FRE-823" else None


class _FakeChannelPoster:
    """Records calls and returns a canned outcome."""

    def __init__(self, outcome: str = "delivered") -> None:
        self.calls: list[tuple[int, str, str]] = []
        self._outcome = outcome

    def __call__(self, port: int, secret: str, payload_json: str) -> str:
        self.calls.append((port, secret, payload_json))
        return self._outcome  # type: ignore[return-value]


def test_run_once_channel_delivery_success_never_consults_scrape(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # AC-1: zero tmux capture-pane/send-keys calls for a channel-mode delivery
    # that succeeds.
    monkeypatch.setitem(
        launcher._TOPOLOGY,
        "build2",
        dataclasses.replace(launcher._TOPOLOGY["build2"], mode="channel"),
    )
    ledger: dict = {}
    runner = _idle_runner()
    poster = _FakeChannelPoster(outcome="delivered")
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [_channel_worker_pr()],
        session_resolver=_resolve_build2,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        ledger=ledger,
        ledger_persist=ledger.update,
        channel_poster=poster,
        channel_secret="s3cret",
    )
    assert not any(call[:2] == ("tmux", "capture-pane") for call in runner.calls)
    assert not any(call[:2] == ("tmux", "send-keys") for call in runner.calls)
    assert len(poster.calls) == 1
    port, secret, payload_json = poster.calls[0]
    assert port == launcher.topology_for("build2").channel_port
    assert secret == "s3cret"
    assert json.loads(payload_json) == build_channel_payload(_channel_worker_pr())
    entry = ledger["worker:412:abc1234def5678"]
    assert entry.transport == "channel"
    assert entry.sent_at is not None
    assert entry.consumed_at is not None


def test_run_once_channel_delivery_failure_falls_back_to_send_keys(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # AC-5 fallback: a channel-down delivery falls back to send-keys rather
    # than silently dropping, and the ledger stays accurate (send_keys,
    # untouched default -- never "corrected" from an optimistic channel tag).
    monkeypatch.setitem(
        launcher._TOPOLOGY,
        "build2",
        dataclasses.replace(launcher._TOPOLOGY["build2"], mode="channel"),
    )
    ledger: dict = {}
    runner = _idle_runner()
    poster = _FakeChannelPoster(outcome="unreachable")
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [_channel_worker_pr()],
        session_resolver=_resolve_build2,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        ledger=ledger,
        ledger_persist=ledger.update,
        channel_poster=poster,
        channel_secret="s3cret",
    )
    sends = [c for c in runner.calls if c[:2] == ("tmux", "send-keys")]
    assert sends  # fell back to tmux
    entry = ledger["worker:412:abc1234def5678"]
    assert entry.transport == "send_keys"
    assert entry.sent_at is not None
    assert entry.consumed_at is not None


def test_run_once_channel_secret_missing_falls_back_without_calling_poster(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setitem(
        launcher._TOPOLOGY,
        "build2",
        dataclasses.replace(launcher._TOPOLOGY["build2"], mode="channel"),
    )
    ledger: dict = {}
    runner = _idle_runner()
    poster = _FakeChannelPoster(outcome="delivered")
    logger = _RecordingLogger()  # type: ignore[name-defined]
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [_channel_worker_pr()],
        session_resolver=_resolve_build2,
        runner=runner,
        persist=lambda _s: None,
        logger=logger,
        execute=True,
        ledger=ledger,
        ledger_persist=ledger.update,
        channel_poster=poster,
        channel_secret=None,
    )
    assert poster.calls == []
    sends = [c for c in runner.calls if c[:2] == ("tmux", "send-keys")]
    assert sends
    assert any(event == "channel_secret_missing" for event, _ in logger.events)
    entry = ledger["worker:412:abc1234def5678"]
    assert entry.transport == "send_keys"


def test_run_once_send_keys_mode_seat_unaffected_by_channel_wiring() -> None:
    # AC-5 first half: the real, un-monkeypatched default (send_keys mode)
    # produces the exact same tmux call sequence as before this ticket.
    ledger: dict = {}
    runner = _idle_runner()
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [_channel_worker_pr()],
        session_resolver=_resolve_build2,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        ledger=ledger,
        ledger_persist=ledger.update,
    )
    sends = [c for c in runner.calls if c[:2] == ("tmux", "send-keys")]
    assert sends == [
        (
            "tmux",
            "send-keys",
            "-t",
            "=cc-2build:0.0",
            "-l",
            "PR #412 failed CI checks - correct them",
        ),
        ("tmux", "send-keys", "-t", "=cc-2build:0.0", "Enter"),
    ]
    entry = ledger["worker:412:abc1234def5678"]
    assert entry.transport == "send_keys"


def test_run_once_channel_delivery_records_single_ledger_entry_no_double_fire(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # AC-4: transport lives on the single per-event-id entry, not a parallel
    # key space -- a future refactor that reintroduces one would break this.
    monkeypatch.setitem(
        launcher._TOPOLOGY,
        "build2",
        dataclasses.replace(launcher._TOPOLOGY["build2"], mode="channel"),
    )
    ledger: dict = {}
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [_channel_worker_pr()],
        session_resolver=_resolve_build2,
        runner=_idle_runner(),
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        ledger=ledger,
        ledger_persist=ledger.update,
        channel_poster=_FakeChannelPoster(outcome="delivered"),
        channel_secret="s3cret",
    )
    assert list(ledger.keys()) == ["worker:412:abc1234def5678"]
    assert ledger["worker:412:abc1234def5678"].transport == "channel"


def test_run_once_channel_crash_recovery_via_reconcile_is_accurately_send_keys(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Regression for codex's High findings: an entry left at
    # send_started_at=None (crash before the send attempt) recovers via the
    # EXISTING, unmodified reconcile() -> tmux retry path, with zero
    # gating_watcher.py changes required. The recovered entry is correctly
    # audited as send_keys -- that is genuinely how it was (re)delivered.
    from scripts.dispatch.trigger_ledger import record_pending

    seeded, _ = record_pending(
        {},
        event_id="worker:412:abc1234def5678",
        source="worker-ci-red",
        target_pane="cc-2build",
        ticket="412",
        command="PR #412 failed CI checks - correct them",
        preconditions={"head_sha": "abc1234def5678"},
        now=50.0,
        ttl_s=60.0,
    )
    monkeypatch.setitem(
        launcher._TOPOLOGY,
        "build2",
        dataclasses.replace(launcher._TOPOLOGY["build2"], mode="channel"),
    )
    runner = _idle_runner()
    ledger = dict(seeded)
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [],  # nothing new -- only reconcile the seeded entry
        session_resolver=_resolve_build2,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        ledger=ledger,
        ledger_persist=ledger.update,
        channel_poster=_FakeChannelPoster(outcome="delivered"),
        channel_secret="s3cret",
    )
    sends = [c for c in runner.calls if c[:2] == ("tmux", "send-keys")]
    assert sends  # recovered via the universal tmux retry path
    assert ledger["worker:412:abc1234def5678"].transport == "send_keys"
    assert ledger["worker:412:abc1234def5678"].consumed_at is not None


# --- post_channel_event / load_channel_secret (FRE-872) ---------------------


class _FakeHttpResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_post_channel_event_200_is_delivered() -> None:
    outcome = post_channel_event(
        8791, "s3cret", "{}", opener=lambda *a, **k: _FakeHttpResponse(200)
    )
    assert outcome == "delivered"


def test_post_channel_event_non_2xx_is_unreachable() -> None:
    outcome = post_channel_event(
        8791, "s3cret", "{}", opener=lambda *a, **k: _FakeHttpResponse(500)
    )
    assert outcome == "unreachable"


def test_post_channel_event_connection_error_is_unreachable() -> None:
    import urllib.error

    def _raise(*a: object, **k: object) -> object:
        raise urllib.error.URLError("connection refused")

    outcome = post_channel_event(8791, "s3cret", "{}", opener=_raise)
    assert outcome == "unreachable"


def test_post_channel_event_timeout_is_unreachable() -> None:
    def _raise(*a: object, **k: object) -> object:
        raise TimeoutError("timed out")

    outcome = post_channel_event(8791, "s3cret", "{}", opener=_raise)
    assert outcome == "unreachable"


def test_load_channel_secret_reads_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AGENT_SESHAT_CHANNEL_SECRET", "from-env")
    assert load_channel_secret() == "from-env"


def test_load_channel_secret_absent_returns_none(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("AGENT_SESHAT_CHANNEL_SECRET", raising=False)
    monkeypatch.chdir("/tmp")
    assert load_channel_secret() is None


# --- _context_pressure_threshold_default (code-review finding, malformed env) ---


def test_context_pressure_threshold_default_unset_env(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("AGENT_CONTEXT_PRESSURE_THRESHOLD", raising=False)
    assert _context_pressure_threshold_default() == DEFAULT_CONTEXT_PRESSURE_THRESHOLD


def test_context_pressure_threshold_default_valid_env(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("AGENT_CONTEXT_PRESSURE_THRESHOLD", "55")
    assert _context_pressure_threshold_default() == 55.0


def test_context_pressure_threshold_default_malformed_env_falls_back(monkeypatch) -> None:  # noqa: ANN001
    # A crash here would take down the whole watcher process (all triggers,
    # not just context-pressure) -- must degrade to the default, never raise.
    monkeypatch.setenv("AGENT_CONTEXT_PRESSURE_THRESHOLD", "not-a-number")
    assert _context_pressure_threshold_default() == DEFAULT_CONTEXT_PRESSURE_THRESHOLD


def test_context_pressure_threshold_default_empty_env_falls_back(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("AGENT_CONTEXT_PRESSURE_THRESHOLD", "")
    assert _context_pressure_threshold_default() == DEFAULT_CONTEXT_PRESSURE_THRESHOLD


# --- context_pressure (AC-1) ------------------------------------------------


def test_context_pressure_below_threshold_excluded() -> None:
    readings = [ContextReading(session="cc-master", ctx=650_000, model="claude-opus-4-8")]  # 65%
    assert context_pressure(readings, threshold=70.0) == []


def test_context_pressure_at_threshold_included() -> None:
    readings = [ContextReading(session="cc-master", ctx=700_000, model="claude-opus-4-8")]  # 70%
    assert context_pressure(readings, threshold=70.0) == [("cc-master", 70.0)]


def test_context_pressure_above_threshold_included() -> None:
    readings = [ContextReading(session="cc-master", ctx=900_000, model="claude-sonnet-5")]  # 90%
    assert context_pressure(readings, threshold=70.0) == [("cc-master", 90.0)]


def test_context_pressure_haiku_window_is_200k_not_1m() -> None:
    # 150k/200k = 75% on haiku's window; would be 15% on a 1M window.
    readings = [ContextReading(session="cc-worker", ctx=150_000, model="claude-haiku-4-5")]
    assert context_pressure(readings, threshold=70.0) == [("cc-worker", 75.0)]


def test_context_pressure_unmapped_model_falls_back_to_default_window() -> None:
    readings = [ContextReading(session="cc-x", ctx=750_000, model="some-unmapped-model")]
    assert context_pressure(readings, threshold=70.0) == [("cc-x", 75.0)]


def test_context_pressure_multiple_readings_filters_independently() -> None:
    readings = [
        ContextReading(session="cc-master", ctx=900_000, model="claude-opus-4-8"),  # 90% -> in
        ContextReading(session="cc-worker", ctx=100_000, model="claude-opus-4-8"),  # 10% -> out
    ]
    assert context_pressure(readings, threshold=70.0) == [("cc-master", 90.0)]


# --- prune_state: context-pressure key shape (design decision) -------------


def test_prune_state_keeps_context_pressure_key_within_ttl() -> None:
    # 2-part key (no PR component) must not be pruned by open-PR membership.
    sent = {"ctxpressure:cc-master": 100.0}
    kept = prune_state(sent, now=200.0, max_ttl_s=600.0, open_prs=[412])
    assert kept == {"ctxpressure:cc-master": 100.0}


def test_prune_state_drops_expired_context_pressure_key() -> None:
    sent = {"ctxpressure:cc-master": 100.0}
    kept = prune_state(sent, now=800.0, max_ttl_s=600.0, open_prs=[412])
    assert kept == {}


# --- run_once: context-pressure nudge (AC-2, AC-3) --------------------------


def _pressure_reading(
    ctx: int = 750_000, model: str = "claude-opus-4-8", session: str = MASTER_SESSION
) -> ContextReading:
    return ContextReading(session=session, ctx=ctx, model=model)  # 75% at default opus window


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    def warning(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


def test_run_once_context_pressure_logs_regardless_of_execute() -> None:
    logger = _RecordingLogger()
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [],
        session_resolver=_no_session,
        runner=_idle_runner(),
        persist=lambda _s: None,
        logger=logger,
        execute=False,
        context_reader=lambda: [_pressure_reading()],
    )
    ctx_events = [f for e, f in logger.events if e == "context_pressure"]
    assert ctx_events == [
        {"trace_id": ctx_events[0]["trace_id"], "session": MASTER_SESSION, "pct": 75.0}
    ]


def test_run_once_context_pressure_dry_run_sends_nothing() -> None:
    runner = _idle_runner()
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [],
        session_resolver=_no_session,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=False,
        context_reader=lambda: [_pressure_reading()],
    )
    assert not any(c[:2] == ("tmux", "send-keys") for c in runner.calls)


def test_run_once_context_pressure_below_threshold_sends_nothing() -> None:
    runner = _idle_runner()
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [],
        session_resolver=_no_session,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        context_reader=lambda: [_pressure_reading(ctx=100_000)],  # 10% << 70% default
    )
    assert not any(c[:2] == ("tmux", "send-keys") for c in runner.calls)


def test_run_once_context_pressure_busy_session_skips() -> None:
    runner = _RecordingRunner(
        {
            ("tmux", "has-session"): _FakeRunResult(returncode=0),
            ("tmux", "capture-pane"): _FakeRunResult(returncode=0, stdout=_BUSY_PANE),
        }
    )
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [],
        session_resolver=_no_session,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        context_reader=lambda: [_pressure_reading()],
    )
    assert not any(c[:2] == ("tmux", "send-keys") for c in runner.calls)


def test_run_once_context_pressure_sends_nudge_when_idle_and_over_threshold() -> None:
    runner = _idle_runner()
    state: dict[str, float] = {}
    run_once(
        state,
        now=100.0,
        board_fetcher=lambda: [],
        session_resolver=_no_session,
        runner=runner,
        persist=lambda st: state.update(st),
        logger=_NullLogger(),
        execute=True,
        context_reader=lambda: [_pressure_reading()],
    )
    send_calls = [c for c in runner.calls if c[:2] == ("tmux", "send-keys") and len(c) == 6]
    assert send_calls == [
        ("tmux", "send-keys", "-t", exact_pane(MASTER_SESSION), "-l", send_calls[0][5])
    ]
    assert send_calls[0][5].startswith("Context at 75% —")
    assert state == {"ctxpressure:cc-master": 100.0}


def test_run_once_context_pressure_dedup_suppresses_second_tick_within_ttl() -> None:
    state: dict[str, float] = {}
    run_once(
        state,
        now=100.0,
        board_fetcher=lambda: [],
        session_resolver=_no_session,
        runner=_idle_runner(),
        persist=lambda st: state.update(st),
        logger=_NullLogger(),
        execute=True,
        context_reader=lambda: [_pressure_reading()],
    )
    assert state == {"ctxpressure:cc-master": 100.0}

    runner2 = _idle_runner()
    run_once(
        dict(state),
        now=150.0,  # well within DEFAULT_MASTER_TTL_S (6h)
        board_fetcher=lambda: [],
        session_resolver=_no_session,
        runner=runner2,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        context_reader=lambda: [_pressure_reading()],
    )
    assert not any(c[:2] == ("tmux", "send-keys") for c in runner2.calls)


def test_run_once_context_pressure_re_arms_after_ttl() -> None:
    state = {"ctxpressure:cc-master": 100.0}
    runner = _idle_runner()
    run_once(
        state,
        now=100.0 + 21600.0 + 1.0,  # past the 6h default TTL
        board_fetcher=lambda: [],
        session_resolver=_no_session,
        runner=runner,
        persist=lambda st: state.update(st),
        logger=_NullLogger(),
        execute=True,
        context_reader=lambda: [_pressure_reading()],
    )
    assert any(c[:2] == ("tmux", "send-keys") for c in runner.calls)


def test_run_once_context_pressure_defaults_do_not_affect_pr_only_ticks() -> None:
    # No context_reader passed -> default (empty) means zero behavior change (AC-4 regression guard).
    runner = _idle_runner()
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [_pr()],
        session_resolver=_no_session,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
    )
    sends = [c for c in runner.calls if c[:2] == ("tmux", "send-keys")]
    assert len(sends) == 2  # exactly the /master trigger's send-keys pair, nothing extra
    assert ("tmux", "send-keys", "-t", "=cc-master:0.0", "-l", "/master 412") in sends


# --- run_once: context-pressure wired through trigger_ledger (FRE-848) -----


def test_run_once_context_pressure_ledger_records_and_consumes_on_send() -> None:
    ledger: dict = {}
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [],
        session_resolver=_no_session,
        runner=_idle_runner(),
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        context_reader=lambda: [_pressure_reading()],
        ledger=ledger,
        ledger_persist=ledger.update,
    )
    assert snapshot_unconsumed(ledger) == ()  # fully closed out
    entry = ledger["ctxpressure:cc-master"]
    assert entry.ticket == "cc-master"
    assert entry.sent_at is not None
    assert entry.consumed_at is not None


def test_run_once_context_pressure_ledger_abandoned_on_busy_session() -> None:
    runner = _RecordingRunner(
        {
            ("tmux", "has-session"): _FakeRunResult(returncode=0),
            ("tmux", "capture-pane"): _FakeRunResult(returncode=0, stdout=_BUSY_PANE),
        }
    )
    ledger: dict = {}
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [],
        session_resolver=_no_session,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        context_reader=lambda: [_pressure_reading()],
        ledger=ledger,
        ledger_persist=ledger.update,
    )
    entry = ledger["ctxpressure:cc-master"]
    assert entry.sent_at is None  # never actually sent
    assert entry.consumed_at is not None  # abandoned -- eligible for a fresh attempt


def test_run_once_context_pressure_ledger_untouched_below_threshold() -> None:
    ledger: dict = {}
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [],
        session_resolver=_no_session,
        runner=_idle_runner(),
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        context_reader=lambda: [_pressure_reading(ctx=100_000)],  # 10% << 70%
        ledger=ledger,
        ledger_persist=ledger.update,
    )
    assert ledger == {}


def test_run_once_context_pressure_ledger_survives_a_pr_prune_tick() -> None:
    # Regression lock for the FRE-849-scoped fix: a ctxpressure ledger entry
    # must not be evicted by open-PR pruning logic just because its ticket
    # ("cc-master") is never a PR number. Simulate the trailing prune a real
    # tick() performs (open_prs from an unrelated PR-only board).
    from scripts.dispatch.trigger_ledger import prune_ledger

    ledger: dict = {}
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [_pr()],  # an unrelated open PR, #412
        session_resolver=_no_session,
        runner=_idle_runner(),
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        context_reader=lambda: [_pressure_reading()],
        ledger=ledger,
        ledger_persist=ledger.update,
    )
    pruned = prune_ledger(ledger, now=100.0, retention_s=7 * 24 * 3600.0, open_prs=[412])
    assert "ctxpressure:cc-master" in pruned


def test_run_once_context_pressure_ledger_reconciles_pending_entry_before_new_decisions() -> None:
    # A crash right after ledger-write (no send ever attempted) must be
    # retried at the top of the next tick -- mirrors the PR-trigger
    # equivalent test.
    from scripts.dispatch.trigger_ledger import record_pending

    seeded, _ = record_pending(
        {},
        event_id="ctxpressure:cc-master",
        source="context-pressure",
        target_pane="cc-master",
        ticket="cc-master",
        command="Context at 75% ...",
        preconditions={},
        now=50.0,
        ttl_s=21600.0,
    )
    runner = _idle_runner()
    ledger = dict(seeded)
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [],
        session_resolver=_no_session,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        context_reader=lambda: [],  # nothing new this tick -- reconcile alone must retry
        ledger=ledger,
        ledger_persist=ledger.update,
    )
    sends = [c for c in runner.calls if c[:2] == ("tmux", "send-keys")]
    assert ("tmux", "send-keys", "-t", "=cc-master:0.0", "-l", "Context at 75% ...") in sends
    assert ledger["ctxpressure:cc-master"].consumed_at is not None


def test_run_once_kill_switch_skips_ledger_reconcile_too() -> None:
    from scripts.dispatch.trigger_ledger import record_pending

    seeded, _ = record_pending(
        {},
        event_id="master:999:deadbeef",
        source="master-ready",
        target_pane="cc-master",
        ticket="999",
        command="/master 999",
        preconditions={"head_sha": "deadbeef"},
        now=50.0,
        ttl_s=600.0,
    )
    ledger = dict(seeded)
    runner = _idle_runner()
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [_pr()],
        session_resolver=_no_session,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=True,
        kill_switch_engaged=lambda: True,
        ledger=ledger,
        ledger_persist=ledger.update,
    )
    assert runner.calls == []
    assert ledger["master:999:deadbeef"].consumed_at is None  # untouched -- not reconciled


def test_run_once_dry_run_does_not_actuate_pending_ledger_entry() -> None:
    """FRE-844: a dry-run after a crashed execute tick must not retry pending entries.

    A manual one-shot dry-run after a prior execute-mode tick crashed should be
    truly inert: no keys sent, no ledger entries actuated. Reconcile must be gated
    behind the execute flag so that crash-recovery only runs in --execute mode.
    """
    from scripts.dispatch.trigger_ledger import record_pending

    # Seed a pending entry (crash right after record_pending, before send).
    seeded, _ = record_pending(
        {},
        event_id="master:999:deadbeef",
        source="master-ready",
        target_pane="cc-master",
        ticket="999",
        command="/master 999",
        preconditions={"head_sha": "deadbeef"},
        now=50.0,
        ttl_s=600.0,
    )
    runner = _idle_runner()
    ledger = dict(seeded)
    # Dry-run after the crash: execute=False
    run_once(
        {},
        now=100.0,
        board_fetcher=lambda: [],  # no new board state
        session_resolver=_no_session,
        runner=runner,
        persist=lambda _s: None,
        logger=_NullLogger(),
        execute=False,  # dry-run: must not send anything
        ledger=ledger,
        ledger_persist=ledger.update,
    )
    # Verify: no send-keys calls at all (reconcile was not executed)
    assert not any(c[:2] == ("tmux", "send-keys") for c in runner.calls)
    # Ledger entry untouched: still pending, never consumed
    assert ledger["master:999:deadbeef"].sent_at is None
    assert ledger["master:999:deadbeef"].consumed_at is None
