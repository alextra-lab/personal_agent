"""Exact-match tmux targeting regression tests (FRE-909).

The 2026-07-17 incident: the launcher's teardown ``kill-session -t cc-build``
killed the LIVE ``cc-build2`` seat mid-build, because tmux resolves an absent
target by prefix and ``cc-build`` is a strict prefix of ``cc-build2``.

These tests pin the *argv* (the runner is a seam, so argv is the observable
contract with tmux). AC-1/2/3 each assert the specific destructive behaviour
cannot recur; AC-4 asserts no call site hand-rolls a raw target again.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from scripts.dispatch.gating_watcher import send_to_session
from scripts.dispatch.tmux_target import exact_pane, exact_session

# The incident's exact shape: a live seat whose name extends the dead one.
_DEAD_SEAT = "cc-build"
_LIVE_NAME_EXTENSION = "cc-build2"


class _Recorder:
    """Runner seam that records argv and replays canned results."""

    def __init__(self, *, returncode: int = 0, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self._returncode = returncode
        self._stdout = stdout

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self._returncode, self._stdout, "")


def _targets(calls: list[list[str]]) -> list[str]:
    """Every value passed after a ``-t`` flag."""
    out: list[str] = []
    for argv in calls:
        for i, token in enumerate(argv):
            if token == "-t" and i + 1 < len(argv):
                out.append(argv[i + 1])
    return out


def test_exact_session_disables_prefix_fallback() -> None:
    """The ``=`` prefix is what stops tmux resolving an absent name by prefix."""
    assert exact_session("cc-build") == "=cc-build"


def test_exact_pane_carries_window_pane_suffix() -> None:
    """A bare ``=name`` is rejected by pane commands (``can't find pane``)."""
    assert exact_pane("cc-build") == "=cc-build:0.0"


def test_ac1_no_teardown_exists_to_prefix_match_a_live_seat() -> None:
    """AC-1 — the incident, now prevented at the root: there IS no teardown.

    Originally this asserted the launcher's teardown used an exact ``=`` target,
    so killing a dead ``cc-build`` could not prefix-resolve onto the live
    ``cc-build2`` (proven live at filing: ``kill-session -t zztest`` returned 0
    and killed ``zztest2``).

    FRE-913 supersedes that guard with a stronger one: the launcher owns **no**
    termination code at all, so there is no kill left to mis-target. The exact
    targets this module provides still matter for every other dispatch command
    (``has-session``, ``list-panes``, ``capture-pane``, ``send-keys``), which the
    remaining tests here and in ``test_launcher.py`` cover.
    """
    from scripts.dispatch.launcher import LaunchPlan, execute_plan

    runner = _Recorder(returncode=0)
    plan = LaunchPlan(
        outcome="launch",
        stream="build1",
        ticket="FRE-1",
        model="sonnet",
        context="clear",
        tmux_session=_DEAD_SEAT,
        worktree="/opt/seshat/.claude/worktrees/build",
        session_id="s1",
        command=("tmux", "new-session", "-d", "-s", _DEAD_SEAT, "-c", "/tmp", "true"),
        card="test",
        reset_worktree=False,
    )
    execute_plan(plan, runner, sleeper=lambda _seconds: None)

    assert not [argv for argv in runner.calls if "kill-session" in argv]
    # Nothing the launcher issues can resolve onto the live name-extension seat.
    for argv in runner.calls:
        assert not any(arg == _LIVE_NAME_EXTENSION for arg in argv)


def test_ac2_has_session_uses_exact_target_no_false_alive() -> None:
    """AC-2 — an absent seat must report absent, not borrow a live seat."""
    runner = _Recorder(returncode=1)  # tmux: no such session
    outcome = send_to_session(_DEAD_SEAT, "/build FRE-1", runner, require_idle=False)

    assert outcome == "absent"
    assert _targets(runner.calls) == [exact_session(_DEAD_SEAT)]
    # Nothing was sent anywhere.
    assert not any("send-keys" in argv for argv in runner.calls)


def test_ac3_send_keys_never_delivers_into_a_name_extension_seat() -> None:
    """AC-3 — the worst case: a command injected into a DIFFERENT worker."""
    runner = _Recorder(returncode=0)
    outcome = send_to_session(_DEAD_SEAT, "/master 42", runner, require_idle=False)

    # The recorder returns an empty pane, which reads busy (fail-safe), so a
    # require_idle=False send is delivered-but-unconfirmed (FRE-939). Either
    # outcome injects the keys — this test is about WHERE they land.
    assert outcome == "queued"
    targets = _targets(runner.calls)
    # Every target is exact-matched...
    assert all(t.startswith("=") for t in targets), targets
    # ...and none can resolve to the live name-extension seat.
    for target in targets:
        assert not target.startswith(f"={_LIVE_NAME_EXTENSION}")
    # send-keys targets are PANE targets — a bare "=name" errors at runtime.
    send_targets = [argv[argv.index("-t") + 1] for argv in runner.calls if "send-keys" in argv]
    assert send_targets == [exact_pane(_DEAD_SEAT), exact_pane(_DEAD_SEAT)]


def test_ac3_idle_guard_reads_the_right_pane() -> None:
    """capture-pane must be an exact PANE target, not a bare session."""
    runner = _Recorder(returncode=0, stdout="")
    send_to_session(_DEAD_SEAT, "/build FRE-1", runner, require_idle=True)

    capture = [argv for argv in runner.calls if "capture-pane" in argv]
    assert capture, "idle guard did not capture a pane"
    assert capture[0][capture[0].index("-t") + 1] == exact_pane(_DEAD_SEAT)


def test_ac4_no_dispatch_call_site_hand_rolls_a_raw_tmux_target() -> None:
    """AC-4 — every tmux target goes through the shared helper.

    Guards the regression at the source level: a new call site that passes a
    bare name after ``-t`` reintroduces the prefix bug, so fail on sight.
    """
    dispatch_dir = Path(__file__).resolve().parents[2] / "scripts" / "dispatch"
    # Capture the token after `-t` and inspect it. (A lookahead after `\s*`
    # is useless here: `\s*` backtracks to zero-width and the lookahead then
    # tests the space, so every line "passes" — the bug this test first had.)
    after_dash_t = re.compile(r'"-t",\s*([^,\)\]]+)')

    offenders: list[str] = []
    for path in sorted(dispatch_dir.glob("*.py")):
        if path.name == "tmux_target.py":
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            match = after_dash_t.search(line)
            if not match:
                continue
            target = match.group(1).strip()
            if target.startswith(("exact_session(", "exact_pane(")):
                continue
            if target.startswith(('"=', 'f"=')):
                continue
            offenders.append(f"{path.name}:{lineno}: {line.strip()}")

    assert not offenders, (
        "raw tmux -t target(s) found — use exact_session()/exact_pane() "
        "(FRE-909):\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("seat", ["cc-build", "cc-build2", "cc-master", "cc-adrs"])
def test_helpers_are_total_over_real_seat_names(seat: str) -> None:
    """Every live seat name yields both guarded target forms."""
    assert exact_session(seat) == f"={seat}"
    assert exact_pane(seat) == f"={seat}:0.0"
