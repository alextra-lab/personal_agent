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

from collections.abc import Sequence

import pytest
from scripts.dispatch.launcher import (
    DEFAULT_CAPABILITIES,
    LauncherCapabilities,
    execute_plan,
    find_warm_session,
    main,
    plan_launch,
    session_id_for,
    stream_for_tmux_session,
    topology_for,
)


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
    runner = _RecordingRunner()  # status returns empty stdout → clean
    plan = plan_launch("build1", "FRE-786", "opus", context_keep=False)
    result = execute_plan(plan, runner)
    assert result.launched is True
    assert result.outcome == "launch"
    # tmux new-session was invoked after the git preflight.
    assert any("new-session" in call for call in runner.calls)


def test_execute_kills_existing_slot_before_new_session() -> None:
    # The persistent tmux slot is torn down before a fresh CLEAR launch, so
    # `new-session` never collides with a still-existing worker session.
    runner = _RecordingRunner()  # clean worktree
    plan = plan_launch("build1", "FRE-786", "opus", context_keep=False)
    result = execute_plan(plan, runner)
    assert result.launched is True
    kill_idx = next(i for i, c in enumerate(runner.calls) if "kill-session" in c)
    new_idx = next(i for i, c in enumerate(runner.calls) if "new-session" in c)
    assert kill_idx < new_idx  # torn down before recreated
    assert any("kill-session" in c and plan.tmux_session in c for c in runner.calls)


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
