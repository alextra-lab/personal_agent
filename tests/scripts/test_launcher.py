# ruff: noqa: D103
"""Unit tests for the ADR-0110 Remote Control launch primitive (FRE-786 Part 2).

Exercises the pure planner (`plan_launch`) and the IO execution seam
(`execute_plan` / `find_warm_session`) against fixtures only — no live
`claude`/`tmux`/`git`. The live owner-in-loop RC dispatch is the ADR's T3
seam (master-owned), per its Testing strategy.

Covers ADR-0110 acceptance criteria carried by FRE-786:
  AC-2  — CLEAR launches a fresh session at the labeled model and invokes the
          skill; KEEP never machine-launches into a fresh/cleared session.
  AC-7a — with programmatic model-set forced off, the launcher emits
          `manual-model-required` (exact model + command) and never launches
          at an unproven model.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import shlex
from collections.abc import Sequence
from pathlib import Path

import pytest
from scripts.dispatch import launcher as launcher_module
from scripts.dispatch.launcher import (
    DEFAULT_CAPABILITIES,
    LauncherCapabilities,
    execute_plan,
    find_warm_session,
    main,
    plan_launch,
    seat_is_busy,
    seat_state,
    session_id_for,
    stream_for_tmux_session,
    topology_for,
)
from scripts.dispatch.tmux_target import exact_pane, exact_session


class _FakeRunResult:
    """Stand-in for subprocess.CompletedProcess with the fields the seam reads."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


class _RecordingRunner:
    """Records every argv it is called with and returns scripted results."""

    def __init__(self, results: dict[str, _FakeRunResult] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._results = results or {}

    def __call__(self, argv: Sequence[str]) -> _FakeRunResult:
        self.calls.append(tuple(argv))
        # Match on a keyword present in the argv (e.g. "status", "new-session").
        for key, result in self._results.items():
            if key in argv:
                return result
        return _FakeRunResult()


# --- topology --------------------------------------------------------------


def test_topology_maps_each_stream() -> None:
    assert topology_for("build1").tmux_session == "cc-build"
    assert topology_for("build1").skill_command == "/build"
    assert topology_for("build2").tmux_session == "cc-build2"
    assert topology_for("adr").skill_command == "/adr"


def test_topology_mode_is_channel_for_every_worker_seat() -> None:
    # FRE-875 Phase B complete: all three live worker seats are cut over to
    # channel-mode delivery (adr → build2 → build1). This guard now pins the
    # post-cutover state — a future accidental flip back to send_keys here would
    # silently revert live dispatch to the retired scrape-gated path.
    for stream in ("build1", "build2", "adr"):
        assert topology_for(stream).mode == "channel"


def test_stream_for_tmux_session_resolves_known_sessions() -> None:
    assert stream_for_tmux_session("cc-build") == "build1"
    assert stream_for_tmux_session("cc-build2") == "build2"
    assert stream_for_tmux_session("cc-adrs") == "adr"


def test_stream_for_tmux_session_unknown_returns_none() -> None:
    assert stream_for_tmux_session("cc-master") is None
    assert stream_for_tmux_session("nope") is None


def test_seed_carries_resolved_ticket() -> None:
    """AC3: the seed carries the orchestrator-resolved ticket, not a stream number."""
    build = plan_launch("build1", "FRE-806", "opus", context_keep=False)
    assert build.command is not None
    assert "/build FRE-806" in " ".join(build.command)
    adr = plan_launch("adr", "FRE-806", "opus", context_keep=False)
    assert adr.command is not None
    assert "/adr FRE-806" in " ".join(adr.command)


def test_unknown_stream_raises() -> None:
    with pytest.raises(ValueError):
        topology_for("nope")


def test_unknown_model_raises() -> None:
    with pytest.raises(ValueError):
        plan_launch("build1", "FRE-1", "gpt-4", context_keep=False)


# --- AC-2 CLEAR: full machine launch ---------------------------------------


def test_clear_full_caps_launches_at_model_with_seed() -> None:
    plan = plan_launch("build1", "FRE-786", "opus", context_keep=False)
    assert plan.outcome == "launch"
    assert plan.context == "clear"
    assert plan.reset_worktree is True
    assert plan.command is not None
    # tmux detached, named, in the stream's worktree; PTY intact (no pipe).
    assert plan.command[:4] == ("tmux", "new-session", "-d", "-s")
    assert "cc-build" in plan.command
    assert ".claude/worktrees/build" in plan.command
    joined = " ".join(plan.command)
    assert "--model opus" in joined
    assert f"--session-id {plan.session_id}" in joined
    assert "/build FRE-786" in joined  # the seed carries the resolved ticket (AC3)
    assert "|" not in joined  # never piped
    # FRE-911: worker seats launch in acceptEdits so they never strand on an
    # edit-permission prompt when the owner cannot reach the seat.
    assert "--permission-mode acceptEdits" in joined


def test_worker_seat_launches_in_accept_edits_permission_mode() -> None:
    """FRE-911 regression: a dispatched seat must not block on an edit prompt.

    A worker runs unattended and the owner may be unable to reach it, so the
    launch command must carry ``--permission-mode acceptEdits`` — the flag and
    its value adjacent, in that order, so claude parses it correctly. The claude
    invocation is the last (shlex-joined) element of the tmux argv, so it is
    split back out before asserting on the flag/value pair.
    """
    plan = plan_launch("build2", "FRE-880", "sonnet", context_keep=False)
    assert plan.command is not None
    inner = shlex.split(plan.command[-1])
    assert "--permission-mode" in inner, "worker seat launched without a permission mode"
    assert inner[inner.index("--permission-mode") + 1] == "acceptEdits"


# --- AC-2 KEEP: never machine-launch, never reset --------------------------


def test_keep_is_manual_continuation_never_launches() -> None:
    plan = plan_launch("build1", "FRE-786", "opus", context_keep=True)
    assert plan.outcome == "manual-continuation"
    assert plan.context == "keep"
    assert plan.command is None
    assert plan.reset_worktree is False


def test_keep_card_names_required_model_and_states_unproven() -> None:
    plan = plan_launch("build1", "FRE-786", "sonnet", context_keep=True)
    assert "sonnet" in plan.card
    assert "not" in plan.card.lower()  # states the launcher has NOT verified/switched it


def test_keep_with_warm_session_carries_the_id() -> None:
    plan = plan_launch("build1", "FRE-786", "opus", context_keep=True, warm_session_id="abc-123")
    assert plan.outcome == "manual-continuation"
    assert plan.session_id == "abc-123"
    assert plan.command is None  # still never a launch


# --- AC-7a: model-set forced off -------------------------------------------


def test_clear_model_set_off_is_manual_model_required() -> None:
    caps = LauncherCapabilities(auto_seed=True, model_set=False)
    plan = plan_launch("build1", "FRE-786", "opus", context_keep=False, capabilities=caps)
    assert plan.outcome == "manual-model-required"
    assert plan.command is None  # never launches at an unproven model
    assert plan.reset_worktree is False
    assert "opus" in plan.card  # names the exact model
    assert "/build FRE-786" in plan.card  # and the exact command


# --- ADR §4 middle degradation: auto-seed off, model-set on -----------------


def test_clear_auto_seed_off_is_prepare_without_seed() -> None:
    caps = LauncherCapabilities(auto_seed=False, model_set=True)
    plan = plan_launch("build1", "FRE-786", "opus", context_keep=False, capabilities=caps)
    assert plan.outcome == "prepare"
    assert plan.command is not None
    joined = " ".join(plan.command)
    assert "--model opus" in joined
    assert "/build FRE-786" not in joined  # no seed positional
    assert "/build FRE-786" in plan.card  # but surfaced for the owner to tap-send


# --- deterministic session id (codex #1) -----------------------------------


def test_session_id_is_deterministic_per_ticket() -> None:
    a = session_id_for("build1", "FRE-786", "opus", "clear")
    b = session_id_for("build1", "FRE-786", "opus", "clear")
    assert a == b


def test_different_ticket_gets_different_session_id() -> None:
    a = session_id_for("build1", "FRE-786", "opus", "clear")
    b = session_id_for("build1", "FRE-999", "opus", "clear")
    assert a != b


def test_different_stream_or_model_gets_different_session_id() -> None:
    base = session_id_for("build1", "FRE-786", "opus", "clear")
    assert session_id_for("build2", "FRE-786", "opus", "clear") != base
    assert session_id_for("build1", "FRE-786", "sonnet", "clear") != base


# --- shell-metacharacter safety (codex #4) ---------------------------------


def test_metacharacter_model_is_rejected() -> None:
    with pytest.raises(ValueError):
        plan_launch("build1", "FRE-1", "opus; rm -rf /", context_keep=False)


def test_launch_command_has_no_unescaped_metacharacters_from_inputs() -> None:
    # A validated model can never introduce a metacharacter; assert the built
    # command carries only the known tier token, shell-safely joined.
    plan = plan_launch("build1", "FRE-786", "haiku", context_keep=False)
    assert plan.command is not None
    assert plan.command[-1].count("claude") == 1


# --- execute_plan seam ------------------------------------------------------


def test_execute_manual_outcome_never_calls_runner() -> None:
    runner = _RecordingRunner()
    plan = plan_launch("build1", "FRE-786", "opus", context_keep=True)  # KEEP
    result = execute_plan(plan, runner)
    assert result.launched is False
    assert runner.calls == []  # never resets, never launches


def test_execute_manual_model_required_never_calls_runner() -> None:
    runner = _RecordingRunner()
    caps = LauncherCapabilities(auto_seed=True, model_set=False)
    plan = plan_launch("build1", "FRE-786", "opus", context_keep=False, capabilities=caps)
    result = execute_plan(plan, runner)
    assert result.launched is False
    assert runner.calls == []


def test_execute_clean_worktree_launches() -> None:
    plan = plan_launch("build1", "FRE-786", "opus", context_keep=False)
    assert plan.session_id is not None
    # FRE-913: a create is only claimed once the new seat is verified to hold the
    # requested Remote-Control name, so the fake seat must report itself registered.
    runner = _SeatRunner(state="absent", agents=_registered(plan.session_id))
    result = execute_plan(plan, runner, sleeper=_no_wait)
    assert result.launched is True
    assert result.outcome == "launch"
    # tmux new-session was invoked after the git preflight.
    assert any("new-session" in call for call in runner.calls)


def test_execute_absent_seat_creates_without_any_teardown() -> None:
    """FRE-913: the create path reaches ``new-session`` with no teardown at all.

    Replaces the FRE-786-era ``test_execute_kills_existing_slot_before_new_session``,
    which asserted the *opposite* (kill-then-recreate). That teardown is the
    2026-07-08 regression this ticket removes: it churned the seat's Remote
    Control registration on every dispatch. The create path is now reached only
    when the seat is ``absent``, where there is nothing to tear down.
    """
    plan = plan_launch("build1", "FRE-786", "opus", context_keep=False, seat="absent")
    assert plan.session_id is not None
    runner = _SeatRunner(state="absent", agents=_registered(plan.session_id))
    result = execute_plan(plan, runner, sleeper=_no_wait)
    assert result.launched is True
    assert any("new-session" in call for call in runner.calls)
    assert not any("kill-session" in call for call in runner.calls)


def test_execute_dirty_worktree_aborts_without_tmux() -> None:
    runner = _RecordingRunner({"status": _FakeRunResult(stdout=" M some_file.py\n")})
    plan = plan_launch("build1", "FRE-786", "opus", context_keep=False)
    result = execute_plan(plan, runner)
    assert result.launched is False
    assert result.outcome == "worktree-dirty"
    assert not any("new-session" in call for call in runner.calls)  # never launched


def test_execute_tmux_failure_is_launch_failed() -> None:
    runner = _RecordingRunner({"new-session": _FakeRunResult(returncode=1)})
    plan = plan_launch("build1", "FRE-786", "opus", context_keep=False)
    result = execute_plan(plan, runner)
    assert result.launched is False
    assert result.outcome == "launch-failed"


# --- find_warm_session (codex #2) ------------------------------------------


def _agents_payload(*cwds: str) -> str:
    import json

    return json.dumps(
        [{"sessionId": f"sess-{i}", "cwd": cwd, "status": "idle"} for i, cwd in enumerate(cwds)]
    )


def test_find_warm_session_single_match() -> None:
    runner = _RecordingRunner(
        {"agents": _FakeRunResult(stdout=_agents_payload("/opt/seshat/.claude/worktrees/build"))}
    )
    assert find_warm_session("build1", runner) == "sess-0"


def test_find_warm_session_zero_match_returns_none() -> None:
    runner = _RecordingRunner({"agents": _FakeRunResult(stdout=_agents_payload("/somewhere/else"))})
    assert find_warm_session("build1", runner) is None


def test_find_warm_session_multiple_match_returns_none() -> None:
    wt = "/opt/seshat/.claude/worktrees/build"
    runner = _RecordingRunner({"agents": _FakeRunResult(stdout=_agents_payload(wt, wt))})
    assert find_warm_session("build1", runner) is None


# --- CLI dry-run ------------------------------------------------------------


def test_cli_dry_run_prints_launch(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--stream", "build1", "--model", "opus", "--ticket", "FRE-786"])
    assert rc == 0
    assert "launch" in capsys.readouterr().out


def test_cli_no_model_set_prints_manual_model_required(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["--stream", "build1", "--model", "opus", "--no-model-set"])
    assert rc == 0
    assert "manual-model-required" in capsys.readouterr().out


def test_cli_keep_prints_manual_continuation(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--stream", "build1", "--model", "opus", "--keep"])
    assert rc == 0
    assert "manual-continuation" in capsys.readouterr().out


def test_default_capabilities_are_both_on() -> None:
    assert DEFAULT_CAPABILITIES.auto_seed is True
    assert DEFAULT_CAPABILITIES.model_set is True


# --- FRE-913: persistent seats — the dispatcher never terminates a seat -----
#
# Backing: FRE-913 (no ADR; owner ruling 3 — restores the pre-2026-07-08
# persistent-seat behaviour). The 2026-07-08 regression (commit 377d0646) made
# every dispatch kill and immediately recreate the seat, churning its Remote
# Control registration so the owner lost mobile visibility of the worker.
#
# Owner directive 2026-07-18, stronger than the ticket text: the dispatcher must
# not merely avoid killing on the happy path, it must not *contain* termination
# code at all. Enforced structurally by
# ``test_launcher_source_contains_no_tmux_termination_verb``.

_TERMINATION_VERBS = ("kill-session", "kill-pane", "kill-server", "kill-window", "respawn-pane")

_WORKTREE = "/opt/seshat/.claude/worktrees/build"
_IDLE_PANE = "some earlier output\n❯\n"
_BUSY_PANE = "● Building… (1m 2s · ↑ 4.1k tokens)\n❯\n"


class _SeatRunner:
    """A fake seat: models tmux liveness, pane text, and RC agent registration.

    Args:
        state: The seat's liveness — ``live`` (session exists, pane runs
            ``claude``), ``absent`` (no tmux session), or ``unhealthy``
            (session exists but the pane runs something else).
        pane: ``changing`` (each capture differs and is idle — a seat that
            processes what it is sent), ``static`` (every capture identical —
            a seat that never processes), or ``busy`` (mid-turn).
        agents: The ``claude agents --json --all`` payload.
    """

    def __init__(
        self,
        *,
        state: str = "live",
        pane: str = "changing",
        agents: Sequence[dict[str, object]] | None = None,
        goes_busy_on_build: bool = True,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._state = state
        self._pane = pane
        # Default: one registered agent for this worktree, idle. Callers pass an
        # explicit list to model a drifted name, a stale agent, or a silent RC.
        if agents is not None:
            self._agents = list(agents)
        elif state == "live":
            self._agents = [
                {"name": "cc-build", "sessionId": None, "cwd": _WORKTREE, "status": "idle"}
            ]
        else:
            self._agents = []
        self._goes_busy_on_build = goes_busy_on_build
        self._build_sent = False
        self._captures = 0

    def _pane_text(self) -> str:
        if self._pane == "busy":
            return _BUSY_PANE
        if self._pane == "static":
            return _IDLE_PANE
        self._captures += 1
        return f"turn {self._captures}\n{_IDLE_PANE}"

    def __call__(self, argv: Sequence[str]) -> _FakeRunResult:
        self.calls.append(tuple(argv))
        argv = list(argv)
        if "send-keys" in argv and "-l" in argv and argv[-1].startswith("/build"):
            self._build_sent = True
        if argv[:2] == ["tmux", "has-session"]:
            return _FakeRunResult(returncode=1 if self._state == "absent" else 0)
        if argv[:2] == ["tmux", "list-panes"]:
            # "<pane_current_command>\t<pane_current_path>" — the path half proves
            # the seat is attached to THIS stream's worktree (FRE-913 review).
            command = "claude" if self._state == "live" else "bash"
            return _FakeRunResult(stdout=f"{command}\t{_WORKTREE}\n")
        if argv[:2] == ["tmux", "capture-pane"]:
            return _FakeRunResult(stdout=self._pane_text())
        if argv[:2] == ["claude", "agents"]:
            agents = [dict(agent) for agent in self._agents]
            if self._build_sent and self._goes_busy_on_build:
                for agent in agents:
                    agent["status"] = "busy"
            return _FakeRunResult(stdout=json.dumps(agents))
        return _FakeRunResult()

    def sent_text(self) -> list[str]:
        """The literal strings send-keys typed into the seat, in order."""
        return [call[-1] for call in self.calls if "send-keys" in call and "-l" in call]


def _registered(session_id: str, name: str = "cc-build") -> list[dict[str, object]]:  # noqa: D103
    return [
        {
            "name": name,
            "sessionId": session_id,
            "cwd": _WORKTREE,
            "status": "idle",
        }
    ]


def _no_wait(_seconds: float) -> None:
    """Sleeper seam that never actually sleeps (tests must not wall-clock)."""


# --- THE regression test ----------------------------------------------------


def test_live_seat_dispatch_never_kills_or_recreates_the_seat() -> None:
    """AC-1: dispatching to a live seat leaves its claude process untouched.

    This is THE regression test for FRE-913. A live seat must see neither a
    teardown nor a ``new-session`` — the running process (and therefore its
    Remote Control registration, which belongs to that process) survives the
    dispatch. Process-identity is what preserves the owner's mobile visibility.
    """
    runner = _SeatRunner(state="live")
    plan = plan_launch("build1", "FRE-913", "sonnet", context_keep=False, seat="live")
    result = execute_plan(plan, runner, sleeper=_no_wait)

    assert result.outcome == "reuse"
    assert result.launched is True
    assert not any("kill-session" in call for call in runner.calls)
    assert not any("new-session" in call for call in runner.calls)


def test_no_seat_state_or_context_ever_issues_a_termination_verb() -> None:
    """The invariant, swept: NO combination of inputs terminates a seat.

    A single happy-path assertion would not hold the owner's rule — this walks
    every seat state x context x capability combination and proves no argv
    carries a tmux termination verb on any of them.
    """
    for state in ("live", "absent", "unhealthy"):
        for context_keep in (True, False):
            for model_set in (True, False):
                runner = _SeatRunner(state=state, agents=[])
                plan = plan_launch(
                    "build1",
                    "FRE-913",
                    "sonnet",
                    context_keep=context_keep,
                    capabilities=LauncherCapabilities(model_set=model_set),
                    seat=state,
                )
                execute_plan(plan, runner, sleeper=_no_wait)
                for call in runner.calls:
                    for verb in _TERMINATION_VERBS:
                        assert verb not in call, f"{verb} issued for {state}/{context_keep}"


def test_launcher_source_contains_no_tmux_termination_verb() -> None:
    """The invariant, structurally: the launcher cannot terminate a seat at all.

    Owner directive (2026-07-18): "Dispatcher should not have terminate tmux
    code. If it does, remove it." Behavioural sweeps only cover the paths a test
    thinks to walk; this pins the *capability*, so a future edit reintroducing a
    teardown fails here even if it is on a path no test exercises. FRE-909's
    incident was a kill that resolved to the WRONG seat and destroyed a live
    worker mid-build — code that cannot kill cannot kill the wrong thing.

    Checks string *literals* via the AST rather than raw text: a tmux argv is
    built from string constants, so real termination code necessarily appears as
    one. Docstrings and comments are excluded — this module documents at length
    why it does not kill seats, and prose naming a verb must not read as using
    it. (Caught live: the first cut of this test failed on its own docstring.)
    """
    tree = ast.parse(Path(launcher_module.__file__).read_text())
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in docstrings
    ]
    for literal in literals:
        for verb in _TERMINATION_VERBS:
            assert verb not in literal, f"launcher.py must not issue tmux {verb}"


# --- reuse path: delivery ---------------------------------------------------


def test_clear_reuse_delivers_clear_then_model_then_build_in_order() -> None:
    """AC-2/AC-3: CLEAR resets context in-session, then dispatches the ticket."""
    runner = _SeatRunner(state="live")
    plan = plan_launch("build1", "FRE-913", "sonnet", context_keep=False, seat="live")
    execute_plan(plan, runner, sleeper=_no_wait)

    assert runner.sent_text() == ["/clear", "/model sonnet", "/build FRE-913"]


def test_reuse_delivery_targets_are_exact_pane_matched() -> None:
    """FRE-909: every delivery target is exact-matched, never prefix-resolved."""
    runner = _SeatRunner(state="live")
    plan = plan_launch("build1", "FRE-913", "sonnet", context_keep=False, seat="live")
    execute_plan(plan, runner, sleeper=_no_wait)

    for call in runner.calls:
        if "send-keys" in call or "capture-pane" in call:
            assert exact_pane("cc-build") in call


def test_busy_seat_is_not_interrupted_mid_turn() -> None:
    """A seat mid-turn is never typed into — nothing is delivered at all."""
    runner = _SeatRunner(state="live", pane="busy", agents=[])  # RC silent → scrape guards
    plan = plan_launch("build1", "FRE-913", "sonnet", context_keep=False, seat="live")
    result = execute_plan(plan, runner, sleeper=_no_wait)

    assert result.outcome == "seat-busy"
    assert result.launched is False
    assert runner.sent_text() == []


def test_unconfirmed_build_is_never_reported_as_a_successful_dispatch() -> None:
    """A /build that the seat never picks up must not be claimed as reuse.

    ``send-keys -l`` echoes the command into the input box, which changes the
    pane *before* Enter is processed — so a text change alone cannot distinguish
    a submitted /build from a lost one. Only the seat actually going busy proves
    it. A seat that never goes busy is a delivery failure, not a dispatch.
    """
    runner = _SeatRunner(state="live", goes_busy_on_build=False)
    plan = plan_launch("build1", "FRE-913", "sonnet", context_keep=False, seat="live")
    result = execute_plan(plan, runner, sleeper=_no_wait)

    assert result.outcome == "delivery-failed"
    assert result.launched is False


def test_a_no_op_clear_on_an_already_empty_seat_still_dispatches() -> None:
    """An idle pane that does not change is a processed no-op, not a failure.

    ``/clear`` on an already-empty conversation redraws to a byte-identical
    screen. Strict change-detection would condemn a correctly-processed command
    and strand the stream, so an idle-and-unchanged pane is accepted for
    non-final commands.
    """
    runner = _SeatRunner(state="live", pane="static")
    plan = plan_launch("build1", "FRE-913", "sonnet", context_keep=False, seat="live")
    result = execute_plan(plan, runner, sleeper=_no_wait)

    assert result.outcome == "reuse"
    assert "/build FRE-913" in runner.sent_text()


def test_dirty_worktree_aborts_clear_reuse_before_any_delivery() -> None:
    """A dirty worktree still aborts a CLEAR dispatch — now before delivering."""

    class _DirtyRunner(_SeatRunner):
        def __call__(self, argv: Sequence[str]) -> _FakeRunResult:
            if "status" in argv:
                self.calls.append(tuple(argv))
                return _FakeRunResult(stdout=" M src/thing.py\n")
            return super().__call__(argv)

    runner = _DirtyRunner(state="live")
    plan = plan_launch("build1", "FRE-913", "sonnet", context_keep=False, seat="live")
    result = execute_plan(plan, runner, sleeper=_no_wait)

    assert result.outcome == "worktree-dirty"
    assert runner.sent_text() == []


# --- KEEP stays manual ------------------------------------------------------


def test_keep_stays_manual_continuation_on_a_live_seat() -> None:
    """KEEP is never machine-auto-launched — unchanged by FRE-913.

    Codex review pushed back on auto-dispatching KEEP into a live seat: it would
    remove a human gate across every worker stream (``manual-continuation`` maps
    to a ``surfaced``/owner-gated record, ``launch`` to an owned in-flight one).
    That is a contract change, not this ticket's fix, so KEEP is untouched.
    """
    runner = _SeatRunner(state="live")
    plan = plan_launch("build1", "FRE-913", "sonnet", context_keep=True, seat="live")
    result = execute_plan(plan, runner, sleeper=_no_wait)

    assert plan.outcome == "manual-continuation"
    assert result.launched is False
    assert runner.sent_text() == []


# --- unhealthy seat: surface, never destroy, never recreate -----------------


def test_unhealthy_seat_is_surfaced_and_left_completely_alone() -> None:
    """A seat whose pane is not ``claude`` is reported, never reclaimed.

    Reclaiming it would mean killing the tmux session to free the name — exactly
    the termination code the owner's rule forbids. Seat lifecycle (recover/reset)
    belongs to ``cc-sessions``; the launcher only dispatches into seats.
    """
    runner = _SeatRunner(state="unhealthy")
    plan = plan_launch("build1", "FRE-913", "sonnet", context_keep=False, seat="unhealthy")
    result = execute_plan(plan, runner, sleeper=_no_wait)

    assert plan.outcome == "seat-unhealthy"
    assert result.launched is False
    assert not any("new-session" in call for call in runner.calls)
    assert runner.sent_text() == []
    assert "cc-sessions" in result.card  # names the tool that owns recovery


# --- seat_state probe -------------------------------------------------------


def test_seat_state_probe_classifies_each_case() -> None:
    topology = topology_for("build1")
    assert seat_state(topology, _SeatRunner(state="live")) == "live"
    assert seat_state(topology, _SeatRunner(state="absent")) == "absent"
    assert seat_state(topology, _SeatRunner(state="unhealthy")) == "unhealthy"


def test_seat_state_probe_uses_exact_match_targets() -> None:
    """FRE-909: an absent seat must not prefix-resolve to cc-build2."""
    # agents=[] forces the pane fallback, so BOTH target forms are exercised:
    # the session probe and the pane probe. A live seat short-circuits on the RC
    # registry and never reaches the pane, which is why this pins the fallback.
    runner = _SeatRunner(state="live", agents=[])
    seat_state(topology_for("build1"), runner)
    assert any(exact_session("cc-build") in call for call in runner.calls)
    assert any(exact_pane("cc-build") in call for call in runner.calls)


# --- create path: registration verified by identity (F2) --------------------


def test_create_path_verifies_registration_by_name_and_session_id() -> None:
    """AC-4: a created seat is verified to hold the REQUESTED RC name."""
    plan = plan_launch("build1", "FRE-913", "sonnet", context_keep=False, seat="absent")
    assert plan.session_id is not None
    runner = _SeatRunner(state="absent", agents=_registered(plan.session_id))
    result = execute_plan(plan, runner, sleeper=_no_wait)

    assert result.outcome == "launch"
    assert result.launched is True


def test_create_path_reports_unverified_when_rc_allocates_a_fallback_name() -> None:
    """F2, observed live: a seat can register under a DIFFERENT name.

    A seat launched as ``--remote-control cc-build`` registered as ``build-41``.
    The original diagnosis (the requested name was still held) was WRONG —
    corrected 2026-07-18 by FRE-914: ``--remote-control``'s optional name
    argument does not set the RC name at all — claude DERIVES it from the cwd
    (session record: ``nameSource=derived``). The launcher now passes the name
    via ``-n``, so this should no longer occur in practice.

    The guard stays regardless: whatever the cause, a seat that registers under
    an unexpected name is alive and working, just invisible where the owner's
    mobile view looks. It is NOT killed and retried — that would destroy a
    healthy claude process and its warm context to fix a visibility problem —
    it is reported.
    """
    plan = plan_launch("build1", "FRE-913", "sonnet", context_keep=False, seat="absent")
    assert plan.session_id is not None
    runner = _SeatRunner(state="absent", agents=_registered(plan.session_id, name="build-41"))
    result = execute_plan(plan, runner, sleeper=_no_wait)

    assert result.outcome == "registration-unverified"
    # The seat IS running and was seeded with the ticket — only its name is
    # wrong. Claiming launched=False would deny the in-flight run stall
    # detection, and the card must not tell the owner to reset it mid-build.
    assert result.launched is True
    assert "build-41" in result.card
    assert "not reset it now" in result.card.lower().replace("do ", "")
    assert not any("kill-session" in call for call in runner.calls)


def test_create_path_rejects_a_stale_agent_holding_the_right_name() -> None:
    """Codex #3: name-match alone could accept a stale agent, not the new seat.

    The launcher passes ``--session-id``, so identity is exactly checkable — the
    verified agent must be the one just launched.
    """
    plan = plan_launch("build1", "FRE-913", "sonnet", context_keep=False, seat="absent")
    runner = _SeatRunner(state="absent", agents=_registered("00000000-dead-dead-dead-000000000000"))
    result = execute_plan(plan, runner, sleeper=_no_wait)

    assert result.outcome == "registration-unverified"


# --- LaunchPlan union invariants (codex #5) ---------------------------------


def test_launch_plan_rejects_two_side_effect_carriers() -> None:
    """A plan carries a create ``command`` XOR reuse ``deliveries``, never both."""
    base = plan_launch("build1", "FRE-913", "sonnet", context_keep=False, seat="absent")
    with pytest.raises(ValueError):
        dataclasses.replace(base, deliveries=("/build FRE-913",))


def test_reuse_plan_must_carry_deliveries() -> None:
    base = plan_launch("build1", "FRE-913", "sonnet", context_keep=False, seat="live")
    assert base.command is None
    assert base.deliveries
    with pytest.raises(ValueError):
        dataclasses.replace(base, deliveries=())


# --- structured readiness beats scraping the rendered TUI -------------------


def _agent(status: str, cwd: str = _WORKTREE) -> dict[str, object]:
    return {"name": "cc-build", "sessionId": "s", "cwd": cwd, "status": status}


def test_seat_is_busy_reads_remote_controls_own_status_field() -> None:
    """Readiness comes from RC's structured status, not a TUI heuristic."""
    topology = topology_for("build1")
    assert seat_is_busy(topology, _SeatRunner(agents=[_agent("busy")])) is True
    assert seat_is_busy(topology, _SeatRunner(agents=[_agent("idle")])) is False


def test_seat_is_busy_returns_none_when_it_cannot_tell() -> None:
    """Never guess: no match, several matches, or an unknown status → None.

    ``None`` is the signal for "fall back to the pane scrape", which is why it
    must be distinguishable from a confident ``False``.
    """
    topology = topology_for("build1")
    assert seat_is_busy(topology, _SeatRunner(agents=[])) is None
    assert seat_is_busy(topology, _SeatRunner(agents=[_agent("busy"), _agent("idle")])) is None
    assert seat_is_busy(topology, _SeatRunner(agents=[_agent("wat")])) is None
    assert seat_is_busy(topology, _SeatRunner(agents=[_agent("idle", cwd="/elsewhere")])) is None


def test_seat_is_busy_matches_by_cwd_not_by_drifting_name() -> None:
    """A seat's RC name can drift (cc-build → build-41); its worktree cannot."""
    agents = [{"name": "build-41", "sessionId": "s", "cwd": _WORKTREE, "status": "busy"}]
    runner = _SeatRunner(agents=agents)
    assert seat_is_busy(topology_for("build1"), runner) is True


def test_structured_busy_status_blocks_delivery_even_if_the_pane_looks_idle() -> None:
    """The structured signal wins: an idle-LOOKING pane does not override it.

    This is the false-idle case the TUI scrape cannot rule out — the pane renders
    its caret box even mid-turn, so text alone can read as ready when the seat is
    actually working.
    """
    runner = _SeatRunner(state="live", pane="static", agents=[_agent("busy")])
    plan = plan_launch("build1", "FRE-913", "sonnet", context_keep=False, seat="live")
    result = execute_plan(plan, runner, sleeper=_no_wait)

    assert result.outcome == "seat-busy"
    assert runner.sent_text() == []


def test_scrape_is_used_only_when_remote_control_cannot_answer() -> None:
    """With no RC status available, the pane heuristic still guards delivery."""
    runner = _SeatRunner(state="live", pane="busy", agents=[])  # RC silent, pane busy
    plan = plan_launch("build1", "FRE-913", "sonnet", context_keep=False, seat="live")
    assert execute_plan(plan, runner, sleeper=_no_wait).outcome == "seat-busy"


def test_malformed_ticket_identifier_is_rejected() -> None:
    """The ticket id is typed into a live acceptEdits seat — assert its shape.

    Linear's own identifiers cannot express anything but ``TEAM-123``, so this
    never rejects a real ticket; it makes that guarantee local instead of
    trusting a remote API to keep its format. Anything that could carry a
    newline, a leading dash, or free text is refused before it reaches a
    keyboard.
    """
    for bad in ("FRE-913; rm -rf /", "FRE-913\n/clear", "--dangerous", "", "fre-913", "FRE-"):
        with pytest.raises(ValueError):
            plan_launch("build1", bad, "sonnet", context_keep=False, seat="live")
    # The real shape still plans normally.
    assert plan_launch("build1", "FRE-913", "sonnet", context_keep=False, seat="live").deliveries


def test_seat_name_is_passed_via_dash_n_not_remote_control_argument() -> None:
    """FRE-914: the RC name goes in ``-n``; ``--remote-control`` stays bare.

    ``--remote-control <name>``'s optional argument does not set the Remote
    Control name — claude derives it from the cwd instead, so seats registered
    as ``build-83`` / ``adrs-2b`` and disappeared from the owner's mobile view.
    Not a regression: the launcher never passed ``-n`` (see the comment at the
    call site). Verified live: the bare flag plus ``-n <name>`` restores it.

    Regression guard — if this ever reverts to the positional form, every
    dispatched seat silently becomes invisible to the owner again.
    """
    plan = plan_launch("build1", "FRE-914", "sonnet", context_keep=False, seat="absent")
    assert plan.command is not None
    inner = shlex.split(plan.command[-1])

    assert "-n" in inner, "seat name must be passed via -n"
    assert inner[inner.index("-n") + 1] == "cc-build"
    # --remote-control must be BARE: the token after it is another flag, never
    # the seat name (which is silently ignored, leaving a cwd-derived name).
    rc = inner.index("--remote-control")
    assert inner[rc + 1].startswith("-"), (
        f"--remote-control must be bare, got {inner[rc + 1]!r} as its argument"
    )
