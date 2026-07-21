#!/usr/bin/env python3
"""Dispatch launch primitive — Remote Control session launcher (FRE-786, ADR-0110 T2).

Given a dispatch stream, its ticket, a model tier, and the ticket's context
flag, decide *how* to start (or refuse to start) that stream's worker session,
and — on ``--execute`` — perform the launch. Mirrors
``scripts/dispatch/next_resolver.py``'s pure/IO split: a pure planner
(``plan_launch``) produces a ``LaunchPlan`` discriminated union that is fully
unit-inspectable, and a thin IO seam (``execute_plan`` with an injectable
runner) performs the side effects.

**Seats are persistent; this module owns no termination code (FRE-913).** A
dispatch *prepares* a seat, it never destroys one. There is no ``kill-session``,
``kill-pane``, or ``respawn-pane`` on any path — the capability is absent, not
merely unused, and a source-level test enforces that.

Why the capability is removed rather than guarded: between 2026-07-08 (commit
377d0646) and FRE-913, every dispatch killed the seat and immediately recreated
it. The new ``claude`` could not reclaim its Remote-Control name before the old
registration was released, so it silently registered under a fallback name
(observed live: a seat launched as ``cc-build`` came up as ``build-41``) — alive
and working, but invisible on the owner's mobile RC view. FRE-909's earlier
incident was the same code killing the *wrong* seat entirely (an absent
``cc-build`` prefix-matched the live ``cc-build2``, losing a build mid-flight).
Code that cannot kill cannot kill the wrong thing. Seat lifecycle (create /
reset / recover) belongs to ``cc-sessions``; this module only dispatches *into*
seats, and that separation is precisely what the two incidents were missing.

So a dispatch resolves to one of two actions, neither destructive — reuse a
``live`` seat by typing into it, or create an ``absent`` one — while an
``unhealthy`` seat (session present, ``claude`` not running) is surfaced and
left alone. Delivery to a live seat is **send-keys**, not the ADR-0116 channel:
``/clear`` and ``/model`` are Claude Code *client* commands interpreted by the
TTY, so channel-delivered text would be inert prose rather than executed
commands. The channel keeps its own role — structured PR/CI gating events the
seat reasons over — unchanged.

The launcher honours the ADR-0110 context contract and graceful degradation
(§2, §4), and — critically — never *claims* a model, context state, or
registration it cannot prove:

- **CLEAR** ticket, seat already **live** → ``reuse``: the ticket is delivered
  into the running seat (``/clear`` → ``/model <tier>`` → ``/build <id>``). The
  common case, and the one that keeps RC registration stable.
- **CLEAR** ticket, seat **absent**, all Remote-Control mechanics available →
  ``launch``: a fresh, correctly-modelled tmux + Remote-Control session, seeded
  with the stream's skill command, then verified to hold the requested RC name.
- Seat **unhealthy** (present but not running ``claude``) → ``seat-unhealthy``:
  a card only. Never reclaimed — reclaiming means terminating a session.
- **CLEAR**, auto-seed unavailable → ``prepare``: a correctly-modelled fresh
  session, with the exact command surfaced for the owner to send.
- **CLEAR**, programmatic model-set unavailable → ``manual-model-required``:
  **no launch** — the card names the exact model and command (AC-7a).
- **KEEP** ticket → ``manual-continuation``: never a fresh/cleared session and
  never a machine launch; the card names the required model (stating the
  launcher has *not* verified/switched it) and continues in the stream's warm
  session only on an exact single match (AC-2 KEEP path).

The three Remote-Control mechanics (auto-seed a first turn, set the model at
launch, poll completion) were proven live in FRE-786 Part 1, so the default
capabilities are both on; ``--no-auto-seed`` / ``--no-model-set`` force them
off to exercise the fallbacks. The **live** owner-in-loop dispatch (session
self-reports its tier, first action invokes the skill, owner answers a prompt
from a device) is the ADR's T3 seam, which master owns — Remote Control
inherently requires the owner's device.

Preconditions for a real launch (recorded per the ticket): Remote Control
entitlement on the claude.ai account, ``claude auth login`` completed on the
VPS, and Remote Control is disabled when ``ANTHROPIC_BASE_URL`` points
off-Anthropic.

Callable by hand::

    python -m scripts.dispatch.launcher --stream build1 --model opus            # dry-run
    python -m scripts.dispatch.launcher --stream build1 --model opus --keep      # KEEP fallback card
    python -m scripts.dispatch.launcher --stream build1 --model opus --no-model-set  # manual-model-required
    python -m scripts.dispatch.launcher --stream build1 --model opus --execute   # actually launch
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shlex
import subprocess  # noqa: S404 - launches trusted, argv-built git/tmux/claude commands
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Literal, Protocol

from scripts.dispatch.pane_state import session_is_idle
from scripts.dispatch.tmux_target import exact_pane, exact_session

# Model tiers the launcher will place on a command line. Validated so no
# free-form value ever reaches the tmux-parsed inner command (codex #4).
_MODELS: frozenset[str] = frozenset({"opus", "sonnet", "haiku"})

# Shape of a Linear issue identifier (``FRE-913``). The ticket id is the only
# externally-sourced value that reaches a seat's keyboard, so its shape is
# asserted locally rather than assumed from the remote API (FRE-913 security
# review). ASCII-only classes throughout — never the Unicode-permissive ``\d``.
_TICKET_RE = re.compile(r"[A-Z][A-Z0-9]*-[0-9]+")

# Permission mode every dispatched worker seat launches in. A worker runs
# unattended and the owner may be unable to reach it (RC can drop), so a seat
# that blocks on an edit-permission prompt strands unadvanceably (FRE-911).
# ``acceptEdits`` auto-approves file edits inside the isolated worktree while
# still gating non-edit actions; worktree isolation + master's PR gate are the
# compensating controls. This is the mode a healthy seat already runs in.
_WORKER_PERMISSION_MODE: str = "acceptEdits"

# Background-task kill switch set in every worker seat's launch environment
# (FRE-922, CC #61568). Claude Code's Bash tool can start commands with
# ``run_in_background``; those background shells have no upper-bound duration and
# no cleanup guarantee (anthropics/claude-code #61568, #38927, #13091, #55893,
# #43944), so an orphaned ``until…/sleep`` poller left alive after a build keeps
# Remote Control reporting the seat ``busy`` while the conversation sits idle at
# its input prompt — wedging the stream's next dispatch (FRE-917 sat undispatched
# ~90 min). A worker seat never legitimately needs a background bash (the build
# skill polls CI/workflow completion in the foreground and otherwise goes idle),
# so disabling the capability at the seat's process start removes the failure
# mode structurally: a seat that cannot start a background shell cannot orphan
# one. Verified a real Claude Code env var against the installed 2.1.215 binary
# (registered in the known-env-var schema; read by a dedicated accessor). It
# gates only the Bash background option — RC status (``claude agents``), MCP
# servers, and the seshat-dispatch channel are separate subsystems, unaffected.
_DISABLE_BACKGROUND_TASKS_ENV: str = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"

# The approved-allowlist channel reference the launcher passes to ``--channels``
# when a seat is cut over to channel-mode delivery (ADR-0116). Resolves to the
# in-repo ``seshat-dispatch`` plugin registered in the local ``seshat-dispatch``
# marketplace; the plugin is loadable only once master has written the managed
# ``channelsEnabled`` + ``allowedChannelPlugins`` policy (its deploy runbook).
_CHANNEL_PLUGIN_REF: str = "plugin:seshat-dispatch@seshat-dispatch"

# Fixed namespace for deterministic, addressable session ids. Derived once from
# a stable URL string (no wall-clock / randomness), so a given dispatch always
# maps to the same id.
_SESSION_NS: uuid.UUID = uuid.uuid5(uuid.NAMESPACE_URL, "https://frenchforest/seshat/dispatch")

PlanOutcome = Literal[
    "launch",
    "prepare",
    "manual-model-required",
    "manual-continuation",
    "reuse",
    "seat-unhealthy",
]
ResultOutcome = Literal[
    "launch",
    "prepare",
    "manual-model-required",
    "manual-continuation",
    "reuse",
    "seat-unhealthy",
    "worktree-dirty",
    "launch-failed",
    "registration-unverified",
    "delivery-failed",
    "seat-busy",
]

# A seat's observed liveness (FRE-913).
#
# ``unhealthy`` exists so the launcher has somewhere to put "the tmux session is
# there but claude is not running in it" **other than** reclaiming it. Reclaiming
# would mean killing the session to free the name, and the dispatcher owns no
# termination code (see module docstring). Seat lifecycle is ``cc-sessions``'s
# job; the launcher only dispatches into seats.
SeatState = Literal["live", "absent", "unhealthy"]

# The command a healthy seat's pane runs. Verified live on the VPS against the
# real launch shape — ``env SESHAT_CHANNEL_PORT=… claude --remote-control …``
# reports ``claude``, because the ``env`` prefix execs away rather than staying
# as the pane's foreground process.
_SEAT_PANE_COMMAND: str = "claude"

# Bounds for the create path's Remote-Control polls and the reuse path's
# delivery confirmation. Deliberately polls for observable state rather than
# sleeping a guessed interval: a fixed settle is what cc-sessions tried
# (five seconds, still insufficient on 2026-07-17).
# Kept deliberately tight: ``execute_plan`` runs SYNCHRONOUSLY inside the
# orchestrator's per-stream tick, so every second spent polling is a second the
# daemon is not servicing other streams. Worst case is bounded at roughly
# 3 x _DELIVERY_TIMEOUT_S on the reuse path and
# _RC_NAME_FREE_TIMEOUT_S + _RC_REGISTERED_TIMEOUT_S on the create path.
_RC_NAME_FREE_TIMEOUT_S: float = 8.0
_RC_REGISTERED_TIMEOUT_S: float = 10.0
_DELIVERY_TIMEOUT_S: float = 10.0
_POLL_INTERVAL_S: float = 0.5

# How many times a single command's ``Enter`` may be re-sent when the command is
# observed still sitting unsubmitted in the input box (FRE-923). Bounded because
# the repair is a nudge, not a loop: if two extra Enters do not submit it, the
# seat is not merely mid-settle and the delivery should fail closed.
_MAX_ENTER_RESUBMITS: int = 2

# Shortest rendered box prefix accepted as "this is my command, truncated".
# Long enough to distinguish ``/clear`` / ``/model`` / ``/build`` from each other
# and from a bare ``/``.
_MIN_BOX_PREFIX: int = 4

# The live input box: a caret followed by whatever is typed but not yet sent.
# ``pane_state`` matches the EMPTY box (a bare caret) to decide idleness; this
# matches the same line to read what is sitting IN it.
_BOX_LINE_RE: re.Pattern[str] = re.compile(r"^\s*❯\s*(?P<text>.*?)\s*$")

# Hard ceiling on any single tmux/claude/git call. Without it a wedged tmux
# server blocks the runner — and therefore the whole dispatch tick — forever,
# which no poll bound above can rescue.
_SUBPROCESS_TIMEOUT_S: float = 15.0


class RunResult(Protocol):
    """The subset of ``subprocess.CompletedProcess`` the seam reads."""

    returncode: int
    stdout: str


class CommandRunner(Protocol):
    """A callable that runs an argv and returns its result (injectable seam)."""

    def __call__(self, argv: Sequence[str]) -> RunResult:
        """Run ``argv`` and return its result."""
        ...


@dataclasses.dataclass(frozen=True)
class StreamTopology:
    """The fixed per-stream launch coordinates.

    Attributes:
        stream: Dispatch stream key (``build1``/``build2``/``adr``).
        worktree: Repo-relative worktree path the session runs in.
        tmux_session: The named tmux session (a local attach seat beside RC).
        skill_command: The base skill command (``/build`` or ``/adr``) — the
            resolved ticket id is appended to form the seed (FRE-806), so the
            worker builds the orchestrator-resolved ticket rather than
            re-deriving a stream's NEXT.
        channel_port: The per-seat localhost port the seat's ``seshat-dispatch``
            channel binds, so one channel-mode seat never collides with another
            (ADR-0116 "one channel per seat"). Only used when channel-mode is on.
        mode: The seat's per-seat delivery mode (FRE-872/875, ADR-0116) —
            ``"channel"`` or ``"send_keys"``, default ``"send_keys"``. This is
            the **single source of truth** for the seat's channel state: it is
            both what ``gating_watcher.decide()`` consults to choose a worker
            trigger's delivery transport AND what the launcher derives its
            channel-wiring from (``plan_launch`` wires ``--channels`` + the
            per-seat port iff ``mode == "channel"``). Because both read this one
            field, a *launch* of the seat can never be shaped inconsistently
            with the mode the watcher delivers by (FRE-875 removed the
            independent per-invocation ``--channels`` flag that made that drift
            possible). The two reads are NOT simultaneous, though: the watcher
            reads ``mode`` **live** each tick, whereas the launcher wires the
            channel only at a seat's **next (re)launch**. So flipping ``mode`` to
            ``"channel"`` starts channel-delivery immediately, while the seat
            becomes channel-wired only when it is relaunched. The window between
            is covered by the FRE-872 fallback — an unreachable channel POST
            degrades to send-keys, never a dropped trigger — but it is a real
            window. Cutover ordering (Phase B) therefore: provision the shared
            ``SESHAT_CHANNEL_SECRET`` into the seat's environment BEFORE flipping
            ``mode`` (an auto-relaunched channel seat missing the secret fails
            closed and likewise degrades to send-keys), and relaunch the seat as
            part of the flip so its channel goes live promptly.
    """

    stream: str
    worktree: str
    tmux_session: str
    skill_command: str
    channel_port: int
    mode: Literal["channel", "send_keys"] = "send_keys"


# Seat names must never be name-extensions of one another (FRE-909 AC-5). tmux
# resolves an unmatched target by PREFIX, so while the seats were ``cc-build``
# and ``cc-build2`` any command aimed at an absent ``cc-build`` silently
# retargeted the live ``cc-build2`` — which destroyed a worker mid-build on
# 2026-07-17, and again during a manual recovery on 2026-07-18.
#
# ``exact_session``/``exact_pane`` close this for every code path. The rename
# closes the AD-HOC path they cannot reach: a human or agent typing
# ``tmux attach -t cc-build`` interactively, with no helper in between. Both
# halves are wanted; neither substitutes for the other.
#
# Only the seat field changes. Stream keys, Linear ``stream:`` labels, worktree
# directories and channel ports are all unchanged.
_TOPOLOGY: dict[str, StreamTopology] = {
    "build1": StreamTopology(
        "build1",
        ".claude/worktrees/build",
        "cc-1build",
        "/build",
        channel_port=8790,
        mode="channel",
    ),
    "build2": StreamTopology(
        "build2",
        ".claude/worktrees/build2",
        "cc-2build",
        "/build",
        channel_port=8791,
        mode="channel",
    ),
    "adr": StreamTopology(
        "adr", ".claude/worktrees/adrs", "cc-adrs", "/adr", channel_port=8792, mode="channel"
    ),
}


def stream_for_tmux_session(session: str) -> str | None:
    """Reverse-map a tmux session name back to its dispatch stream key.

    Lets a caller that only has the tmux session name (e.g. the resolved
    target of a worker trigger) recover the full ``StreamTopology`` — its
    ``mode``, ``channel_port``, etc. — via ``topology_for``, without changing
    any existing ``str | None`` session-resolver signature.

    Args:
        session: A tmux session name (e.g. ``"cc-build2"``).

    Returns:
        The matching stream key (e.g. ``"build2"``), or ``None`` if no known
        stream's topology uses that session name.
    """
    for stream, topology in _TOPOLOGY.items():
        if topology.tmux_session == session:
            return stream
    return None


def seed_command(topology: StreamTopology, ticket: str) -> str:
    """Return the seed command carrying the resolved ticket (FRE-806, AC3).

    The orchestrator has already resolved the stream's NEXT, so the seed passes
    that ticket to the worker explicitly (``/build FRE-806`` / ``/adr FRE-806``)
    rather than re-deriving it. The build/adr skills accept an explicit
    ``FRE-…`` id, and the auto-seed path is CLEAR-only by construction (KEEP
    tickets return ``manual-continuation`` and never reach here), so the
    explicit-id CLEAR semantics are correct for it.

    Args:
        topology: The stream's launch coordinates.
        ticket: The orchestrator-resolved ticket identifier (e.g. ``FRE-806``).

    Returns:
        The seed command, e.g. ``/build FRE-806``.
    """
    return f"{topology.skill_command} {ticket}"


@dataclasses.dataclass(frozen=True)
class LauncherCapabilities:
    """Which undocumented Remote-Control mechanics are available.

    Both default on — proven live in FRE-786 Part 1. Forced off to exercise the
    ADR §4 fallbacks (and AC-7a).

    Channel-mode is **not** a capability here: whether a seat is channel-wired is
    derived from its ``StreamTopology.mode`` (the single source of truth), not
    from a per-invocation flag — see ``StreamTopology.mode`` (FRE-875).

    Attributes:
        auto_seed: Whether a launch can seed a slash command as the first turn.
        model_set: Whether the model tier can be set programmatically at launch.
    """

    auto_seed: bool = True
    model_set: bool = True


DEFAULT_CAPABILITIES = LauncherCapabilities()


@dataclasses.dataclass(frozen=True)
class LaunchPlan:
    """A fully-decided, side-effect-free launch decision.

    Attributes:
        outcome: The decided plan outcome (discriminated union).
        stream: Dispatch stream key.
        ticket: The ticket identifier (e.g. ``FRE-786``).
        model: The resolved, validated model tier.
        context: ``clear`` or ``keep`` — the ticket's context contract.
        tmux_session: The stream's named tmux session.
        worktree: The stream's repo-relative worktree.
        session_id: Deterministic session id for a launch/prepare, the resolved
            warm session id for a manual-continuation, else ``None``.
        command: The tmux argv to run, or ``None`` for a manual outcome.
        reset_worktree: Whether execution should preflight the worktree (CLEAR
            launch/prepare only — never for KEEP).
        card: The device-visible message; for manual outcomes this is the
            deliverable.
    """

    outcome: PlanOutcome
    stream: str
    ticket: str
    model: str
    context: Literal["clear", "keep"]
    tmux_session: str
    worktree: str
    session_id: str | None
    command: tuple[str, ...] | None
    reset_worktree: bool
    card: str
    deliveries: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Enforce exactly one side-effect carrier for the plan's outcome.

        ``command`` (create a seat) and ``deliveries`` (type into a live seat)
        are two different ways to act on the world hanging off one dataclass, so
        an outcome carrying both — or a ``reuse`` carrying neither — is a
        half-decided plan that would execute ambiguously. Raising here keeps the
        discriminated union honest at construction rather than at execution.

        Raises:
            ValueError: The outcome's carriers are inconsistent.
        """
        if self.outcome == "reuse":
            if self.command is not None:
                raise ValueError("a reuse plan must not carry a create command")
            if not self.deliveries:
                raise ValueError("a reuse plan must carry deliveries")
            return
        if self.deliveries:
            raise ValueError(f"{self.outcome!r} must not carry deliveries")
        if self.outcome in {"launch", "prepare"} and self.command is None:
            raise ValueError(f"{self.outcome!r} requires a create command")
        if self.outcome not in {"launch", "prepare"} and self.command is not None:
            raise ValueError(f"{self.outcome!r} must not carry a create command")


@dataclasses.dataclass(frozen=True)
class LaunchResult:
    """The outcome of executing a plan.

    Attributes:
        outcome: The execution outcome (may differ from the plan outcome when a
            preflight aborts or tmux fails).
        card: The device-visible message describing what happened.
        launched: Whether a session was actually started.
    """

    outcome: ResultOutcome
    card: str
    launched: bool


def known_streams() -> tuple[str, ...]:
    """Return every known dispatch stream key, sorted.

    The single source of truth for "is this a real stream?" — callers that take
    a stream from a human (a CLI flag, a label) validate against this rather
    than silently treating a typo as a stream with no work. ``--stream adrs``
    (the worktree/seat spelling) is NOT the ``adr`` stream key, and answering
    ``none`` for it reports "no work queued" when the truth is "no such
    stream" (2026-07-18).

    Returns:
        The stream keys, sorted for stable CLI help and error text.
    """
    return tuple(sorted(_TOPOLOGY))


def topology_for(stream: str) -> StreamTopology:
    """Return the launch topology for a dispatch stream.

    Args:
        stream: The dispatch stream key (``build1``/``build2``/``adr``).

    Returns:
        The stream's ``StreamTopology``.

    Raises:
        ValueError: The stream is not a known dispatch stream.
    """
    try:
        return _TOPOLOGY[stream]
    except KeyError as exc:
        raise ValueError(f"unknown dispatch stream: {stream!r}") from exc


def session_id_for(stream: str, ticket: str, model: str, context: str) -> str:
    """Return a deterministic, addressable session id for a dispatch.

    Includes the ticket id so two dispatches on the same stream/model/context
    get distinct ids (codex #1) — the id is addressable in ``claude agents``
    and never resumes a prior ticket's context.

    Args:
        stream: Dispatch stream key.
        ticket: Ticket identifier.
        model: Model tier.
        context: ``clear`` or ``keep``.

    Returns:
        A UUID string.
    """
    return str(uuid.uuid5(_SESSION_NS, f"{stream}:{ticket}:{model}:{context}"))


def _build_tmux_command(
    topology: StreamTopology,
    model: str,
    session_id: str,
    seed: str | None,
    *,
    channels: bool = False,
) -> tuple[str, ...]:
    """Build the detached tmux launch argv.

    The final element is the claude invocation, which tmux parses as a shell
    command — so it is assembled with ``shlex.join`` over a validated argv,
    never string-concatenated (codex #4), and never piped (PTY intact).

    When ``channels`` is on, the inner command is prefixed with an ``env`` that
    sets the seat's per-seat ``SESHAT_CHANNEL_PORT`` and suffixed with the
    approved ``--channels`` allowlist reference (ADR-0116). The shared secret is
    deliberately **not** placed on the command line — it would be visible in
    ``ps``/the plan JSON; it is provisioned out-of-band into the seat's
    environment (``SESHAT_CHANNEL_SECRET``) by the deploy runbook, and the
    channel server reads it there (failing closed if absent). Every worker seat
    — channel or send_keys — is additionally launched under an ``env`` prefix
    carrying ``CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`` (FRE-922). When
    ``channels`` is off the argv is otherwise the pre-channel shape (ADR-0116 §5),
    the only addition being that background-tasks env prefix.

    Args:
        topology: The stream's launch coordinates.
        model: The validated model tier.
        session_id: The deterministic session id.
        seed: The skill command to seed as the first turn, or ``None`` to start
            without a seed (the ``prepare`` path).
        channels: Whether to wire the approved channel-mode allowlist path.

    Returns:
        The ``tmux new-session`` argv.
    """
    inner_argv = [
        "claude",
        # ``--remote-control`` stays BARE (it only enables RC). The seat name
        # goes in ``-n`` (FRE-914). The positional form ``--remote-control
        # <name>`` does NOT set the RC name: claude derives it from the cwd
        # instead (session record: ``nameSource=derived``), so seats registered
        # as ``build-83`` / ``adrs-2b`` instead of ``cc-build`` / ``cc-adrs``
        # and vanished from the owner's mobile Remote Control view while
        # running perfectly well.
        #
        # This was never a regression: ``git log -S '"-n"'`` on this file is
        # empty — the launcher used the positional form from its first commit
        # (8277c66c) and never passed ``-n``. The bug only surfaced once the
        # launcher, rather than the owner, started the seats; the owner had
        # always launched with ``-n`` by hand, and cc-master kept its correct
        # name by inheriting it across ``-c`` resumes.
        #
        # Verified live 2026-07-18: bare flag plus ``-n`` restores the name.
        "--remote-control",
        "-n",
        topology.tmux_session,
        "--model",
        model,
        "--session-id",
        session_id,
        # A dispatched worker runs unattended — the owner may not be able to
        # reach the seat (RC can drop). Without this, a fresh seat blocks on its
        # first file-edit permission prompt and strands, unadvanceable, until
        # master send-keys it (2026-07-17 incident, FRE-911). acceptEdits
        # auto-approves edits inside the isolated worktree — the same mode a
        # working seat already runs in — while non-edit actions still gate. The
        # worktree isolation + master's PR gate are the compensating controls.
        "--permission-mode",
        _WORKER_PERMISSION_MODE,
    ]
    # The seed is a positional and must be appended BEFORE the channel flag:
    # ``--channels`` is variadic (it accepts several space-separated plugin refs),
    # so a seed placed after ``--channels <ref>`` would be swallowed as a second
    # channel reference and never run as the first turn. Ordering here is the
    # exact argv master live-verifies against the (undocumented) flag's parser.
    if seed is not None:
        inner_argv.append(seed)
    # Every worker seat launches with background tasks disabled (FRE-922, CC
    # #61568) so it can never orphan a ``run_in_background`` poller that wedges
    # the stream. The channel port (non-secret) joins the same ``env`` prefix
    # when channel-mode is on; the shared secret never touches the command line.
    env_assignments = [f"{_DISABLE_BACKGROUND_TASKS_ENV}=1"]
    if channels:
        env_assignments.append(f"SESHAT_CHANNEL_PORT={topology.channel_port}")
    inner_argv = ["env", *env_assignments, *inner_argv]
    if channels:
        # Append the allowlist ref last (variadic — must follow the seed).
        inner_argv += ["--channels", _CHANNEL_PLUGIN_REF]
    inner = shlex.join(inner_argv)
    return (
        "tmux",
        "new-session",
        "-d",
        "-s",
        topology.tmux_session,
        "-c",
        topology.worktree,
        inner,
    )


def _launch_card(topology: StreamTopology, model: str, session_id: str, dispatch: str) -> str:
    """Card summarising a full machine launch."""
    return (
        f"[{topology.stream}] launch → {topology.tmux_session}: {model} · "
        f"{dispatch} (session {session_id[:8]}); "
        f"attach locally: tmux attach -t {topology.tmux_session}"
    )


def _prepare_card(topology: StreamTopology, model: str, dispatch: str) -> str:
    """Card for a prepared-but-unseeded session (auto-seed unavailable)."""
    return (
        f"[{topology.stream}] prepare → {topology.tmux_session} started at {model}; "
        f"send {dispatch} to begin"
    )


def _manual_model_card(topology: StreamTopology, model: str, dispatch: str) -> str:
    """Card for the model-set-unavailable fallback (AC-7a)."""
    return (
        f"[{topology.stream}] manual-model-required → set the model to {model}, then run "
        f"{dispatch} (the launcher did not set the model and will not launch "
        f"at an unproven model)"
    )


def _reuse_card(topology: StreamTopology, model: str, dispatch: str) -> str:
    """Card for the FRE-913 happy path — a live seat reused, never restarted."""
    return (
        f"[{topology.stream}] reuse → {topology.tmux_session} kept alive at {model}; "
        f"delivered {dispatch} in-session (seat process and Remote Control "
        f"registration untouched)"
    )


def _seat_unhealthy_card(topology: StreamTopology, dispatch: str) -> str:
    """Card for a seat whose tmux session exists but is not running claude.

    Names ``cc-sessions`` explicitly: seat lifecycle is its job, not the
    dispatcher's, and the launcher deliberately owns no way to reclaim the slot.
    """
    return (
        f"[{topology.stream}] seat-unhealthy → {topology.tmux_session} exists but is not "
        f"running claude; the launcher does not reclaim seats. Recover it with "
        f"cc-sessions, then dispatch {dispatch}"
    )


def _registration_unverified_card(
    topology: StreamTopology, registered_as: str | None, dispatch: str
) -> str:
    """Card for a created seat that did not take the requested RC name (F2).

    The seat is left RUNNING: it is alive and working, merely registered under
    the wrong name, and killing a healthy process to chase a nicer name would
    destroy warm context to fix a visibility problem.
    """
    actual = f"registered as {registered_as}" if registered_as else "not registered"
    return (
        f"[{topology.stream}] registration-unverified → seat started but {actual}, "
        f"not {topology.tmux_session}. It is RUNNING and already building {dispatch}; "
        f"only Remote Control visibility is degraded. Do NOT reset it now — that would "
        f"kill work in flight. Reclaim the name with cc-sessions once this run lands"
    )


def _manual_continuation_card(
    topology: StreamTopology, model: str, warm_session_id: str | None, dispatch: str
) -> str:
    """Card for the KEEP path — always manual, never a fresh session (AC-2 KEEP)."""
    if warm_session_id is not None:
        target = f"continue in warm session {warm_session_id[:8]} ({topology.worktree})"
    else:
        target = f"no single warm session found for {topology.worktree}; attach/continue manually"
    return (
        f"[{topology.stream}] manual-continuation → {target}; required model {model} (the launcher "
        f"has NOT verified or switched it); run {dispatch}"
    )


def plan_launch(
    stream: str,
    ticket: str,
    model: str,
    *,
    context_keep: bool,
    capabilities: LauncherCapabilities = DEFAULT_CAPABILITIES,
    warm_session_id: str | None = None,
    seat: SeatState = "absent",
) -> LaunchPlan:
    """Decide how to launch (or refuse to launch) a stream's worker session.

    Pure and side-effect-free — the returned ``LaunchPlan`` fully describes the
    decision and is the unit-test proof surface for AC-2 and AC-7a.

    Args:
        stream: Dispatch stream key (``build1``/``build2``/``adr``).
        ticket: Ticket identifier (e.g. ``FRE-786``).
        model: Model tier — must be one of ``opus``/``sonnet``/``haiku``.
        context_keep: ``True`` for a ``context:keep`` ticket (warm context),
            ``False`` for the CLEAR default (fresh session).
        capabilities: Which Remote-Control mechanics are available.
        warm_session_id: The stream's warm session id, if resolved (KEEP only).
        seat: The seat's observed liveness (``seat_state``). A ``live`` seat is
            reused in-session and never recreated (FRE-913); ``unhealthy``
            surfaces a card and touches nothing. Defaults to ``absent`` so a
            plan-only caller still describes a create.

    Returns:
        The decided ``LaunchPlan``.

    Raises:
        ValueError: The stream or model is not recognised.
    """
    topology = topology_for(stream)
    if model not in _MODELS:
        raise ValueError(f"unknown model tier: {model!r} (expected one of {sorted(_MODELS)})")
    # The ticket id is the one value on this path that originates outside the
    # process (Linear's API), and it ends up TYPED INTO a live session running at
    # ``acceptEdits``. Linear's own identifiers cannot express anything but
    # ``TEAM-123``, so this never rejects a real ticket — it just makes that
    # guarantee local, instead of trusting a remote API to keep its format.
    if not _TICKET_RE.fullmatch(ticket):
        raise ValueError(f"malformed ticket identifier: {ticket!r} (expected e.g. 'FRE-913')")

    # The seed carries the orchestrator-resolved ticket (FRE-806, AC3).
    dispatch = seed_command(topology, ticket)

    if context_keep:
        # KEEP is never machine-auto-launched and never reset/cleared (ADR §2).
        return LaunchPlan(
            outcome="manual-continuation",
            stream=stream,
            ticket=ticket,
            model=model,
            context="keep",
            tmux_session=topology.tmux_session,
            worktree=topology.worktree,
            session_id=warm_session_id,
            command=None,
            reset_worktree=False,
            card=_manual_continuation_card(topology, model, warm_session_id, dispatch),
        )

    # CLEAR: cannot prove the model → refuse to launch (AC-7a).
    if not capabilities.model_set:
        return LaunchPlan(
            outcome="manual-model-required",
            stream=stream,
            ticket=ticket,
            model=model,
            context="clear",
            tmux_session=topology.tmux_session,
            worktree=topology.worktree,
            session_id=None,
            command=None,
            reset_worktree=False,
            card=_manual_model_card(topology, model, dispatch),
        )

    # CLEAR + a LIVE seat: reuse it in-session. The seat's process — and with it
    # the Remote-Control registration the owner's mobile view follows — is never
    # touched. This is the FRE-913 happy path and by far the common case.
    if seat == "live":
        return LaunchPlan(
            outcome="reuse",
            stream=stream,
            ticket=ticket,
            model=model,
            context="clear",
            tmux_session=topology.tmux_session,
            worktree=topology.worktree,
            session_id=None,
            command=None,
            # The dirty-worktree preflight still guards a CLEAR dispatch: reuse
            # does not make uncommitted work any safer to walk over.
            reset_worktree=True,
            card=_reuse_card(topology, model, dispatch),
            # The model switch is unconditional: a live seat's current tier has
            # no reliable probe (``claude agents --json`` does not report it),
            # and ``/model`` is idempotent — always sending it is strictly safer
            # than skipping it on an unprovable comparison, which risks a seat
            # silently building at the wrong tier.
            deliveries=("/clear", f"/model {model}", dispatch),
        )

    # A seat that exists but is not running claude is reported, never reclaimed
    # — reclaiming it means killing the tmux session to free the name.
    if seat == "unhealthy":
        return LaunchPlan(
            outcome="seat-unhealthy",
            stream=stream,
            ticket=ticket,
            model=model,
            context="clear",
            tmux_session=topology.tmux_session,
            worktree=topology.worktree,
            session_id=None,
            command=None,
            reset_worktree=False,
            card=_seat_unhealthy_card(topology, dispatch),
        )

    # CLEAR + model-set available: launch (seeded) or prepare (owner sends the command).
    session_id = session_id_for(stream, ticket, model, "clear")
    seed = dispatch if capabilities.auto_seed else None
    # Channel-wiring is derived from the seat's mode (single source of truth,
    # FRE-875) — never an independent flag, so the launch shape cannot drift
    # from the delivery mode the watcher reads.
    command = _build_tmux_command(
        topology, model, session_id, seed, channels=topology.mode == "channel"
    )
    if capabilities.auto_seed:
        outcome: PlanOutcome = "launch"
        card = _launch_card(topology, model, session_id, dispatch)
    else:
        outcome = "prepare"
        card = _prepare_card(topology, model, dispatch)
    return LaunchPlan(
        outcome=outcome,
        stream=stream,
        ticket=ticket,
        model=model,
        context="clear",
        tmux_session=topology.tmux_session,
        worktree=topology.worktree,
        session_id=session_id,
        command=command,
        reset_worktree=True,
        card=card,
    )


def subprocess_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Default runner: run an argv, capturing output, never via a shell.

    Bounded by ``_SUBPROCESS_TIMEOUT_S``. A timeout is reported as a non-zero
    result rather than raised, so a wedged ``tmux`` degrades to "this probe
    failed" — which every caller already handles — instead of hanging the
    orchestrator tick that called it.
    """
    try:
        return subprocess.run(  # noqa: S603 - argv-built from validated inputs, no shell
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), returncode=124, stdout="", stderr="timeout")


def seat_state(topology: StreamTopology, runner: CommandRunner) -> SeatState:
    """Classify a seat's liveness without touching it (FRE-913).

    Three states, only two of which lead to an action — and neither action
    destroys anything. A seat whose tmux session exists but whose pane is not
    running ``claude`` is deliberately NOT reclaimed: reclaiming means killing
    the session to free the name, and the launcher owns no termination code.

    Exact-match targets throughout (FRE-909) so an absent ``cc-build`` can never
    prefix-resolve onto the live ``cc-build2``.

    Args:
        topology: The stream's launch coordinates.
        runner: The command runner seam.

    Returns:
        ``live`` (reuse it), ``absent`` (safe to create), or ``unhealthy``
        (surface it; touch nothing).
    """
    if runner(["tmux", "has-session", "-t", exact_session(topology.tmux_session)]).returncode != 0:
        return "absent"

    # Primary signal: Remote Control's own registry. An agent registered against
    # this stream's worktree proves BOTH that a claude is alive and that it is
    # attached to the right tree — the two things reuse depends on.
    agents = _rc_agents(runner)
    if agents is not None and any(
        _cwd_matches(str(agent.get("cwd", "")), topology.worktree) for agent in agents
    ):
        return "live"

    # Fallback: the pane itself. Requires the foreground command to be claude AND
    # the pane's path to be this stream's worktree.
    #
    # The worktree check is not optional. The deleted create path pinned cwd with
    # ``new-session -c <worktree>`` on every dispatch, so reuse silently inherited
    # a guarantee that no longer exists: a ``cc-build`` whose claude was started
    # somewhere else (owner ran cc-sessions from the repo root) looks identical by
    # session name, and a ``/build`` typed into it would commit against the wrong
    # tree entirely.
    panes = runner(
        [
            "tmux",
            "list-panes",
            "-t",
            exact_pane(topology.tmux_session),
            "-F",
            "#{pane_current_command}\t#{pane_current_path}",
        ]
    )
    command, _, path = panes.stdout.strip().partition("\t")
    if command == _SEAT_PANE_COMMAND and _cwd_matches(path, topology.worktree):
        return "live"
    return "unhealthy"


def _capture_pane(session: str, runner: CommandRunner) -> str:
    """Return the seat pane's current visible text."""
    return runner(["tmux", "capture-pane", "-t", exact_pane(session), "-p"]).stdout


def _rc_agents(runner: CommandRunner) -> list[dict[str, object]] | None:
    """Return the Remote-Control agent registry, or ``None`` if unreadable.

    ``None`` and ``[]`` mean genuinely different things and must not be
    conflated: ``[]`` is "RC answered, no agents are registered" (evidence),
    while ``None`` is "RC could not be read at all" — a non-zero exit, a warning
    line before the JSON, an older CLI without ``--all``. Treating unreadable as
    empty would let an environment problem masquerade as a registration failure
    and condemn a perfectly healthy seat.
    """
    result = runner(["claude", "agents", "--json", "--all"])
    if getattr(result, "returncode", 0) != 0:
        return None
    try:
        raw: object = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(raw, dict):
        candidate = raw.get("agents") or raw.get("sessions") or []
        raw = candidate
    if not isinstance(raw, list):
        return None
    return [agent for agent in raw if isinstance(agent, dict)]


def _poll_until(
    predicate: Callable[[], bool],
    *,
    timeout_s: float,
    sleeper: Callable[[float], None],
) -> bool:
    """Poll ``predicate`` until true or the bound elapses.

    Polls observable state rather than sleeping a guessed interval — the ticket's
    own finding is that a fixed settle is a guess (cc-sessions' five seconds was
    still insufficient on 2026-07-17).

    Args:
        predicate: The condition to wait for; called at least once.
        timeout_s: Upper bound on total wait.
        sleeper: The sleep seam (injectable so tests never wall-clock).

    Returns:
        Whether the predicate became true within the bound.
    """
    attempts = max(1, int(timeout_s / _POLL_INTERVAL_S))
    for attempt in range(attempts):
        if predicate():
            return True
        if attempt < attempts - 1:
            sleeper(_POLL_INTERVAL_S)
    return False


def _box_contents(pane_text: str) -> str:
    """Return the text sitting unsubmitted in the seat's input box.

    Reads the LAST caret line of the pane — the live input box renders below any
    scrollback, and an earlier ``❯`` in transcript history must never be mistaken
    for it. ``tmux capture-pane -p`` is used without ``-e``, so the text carries
    no ANSI escapes to strip (verified live, FRE-923).

    Args:
        pane_text: The ``tmux capture-pane -p`` output.

    Returns:
        The box's contents, or ``""`` when the box is empty or unreadable.
    """
    for line in reversed(pane_text.splitlines()):
        match = _BOX_LINE_RE.match(line)
        if match:
            return match.group("text").strip()
    return ""


def _pending_in_box(pane_text: str, command: str) -> bool:
    """Whether ``command`` is observably typed-but-unsubmitted in the input box.

    Deliberately tolerant rather than an exact full-line equality: a pane can
    wrap or truncate a long command, so a rendered box holding a *prefix* of the
    command (or a command that starts with the rendered text) still counts. The
    match is one-directional-either-way on purpose — the dispatch commands
    (``/clear``, ``/model <tier>``, ``/build <FRE>``) share no prefix, so this
    cannot confuse one for another.

    Args:
        pane_text: The ``tmux capture-pane -p`` output.
        command: The command just typed into the seat.

    Returns:
        Whether the box still holds this command, unsubmitted.
    """
    contents = _box_contents(pane_text)
    if not contents:
        return False
    if contents.startswith(command):
        return True
    # Truncation tolerance, floored: every dispatch command begins with ``/``, so
    # accepting any prefix would let a single stray ``/`` in the box read as
    # "my command is pending" and earn a needless Enter. Require enough
    # characters to actually identify the command.
    return command.startswith(contents) and len(contents) >= _MIN_BOX_PREFIX


def deliver_to_seat(
    plan: LaunchPlan,
    runner: CommandRunner,
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> ResultOutcome:
    """Type a plan's commands into a live seat, confirming each is processed.

    Never restarts, clears-by-recreation, or otherwise touches the seat's
    process — the whole point of FRE-913 is that the ``claude`` process (and
    therefore its Remote-Control registration) survives a dispatch untouched.

    Readiness is read from Remote Control's own ``status`` field where it can be
    (a structured signal from the tool itself), falling back to the rendered-TUI
    scrape only when RC cannot answer — the scrape has no supported contract and
    has produced both false-busy (FRE-845) and false-idle readings.

    Two ordering hazards are guarded, both of which silently lose the dispatch:

    - **A seat mid-turn** is not typed into at all (mirrors the watcher's
      worker-trigger discipline), so a build in progress is never interrupted.
    - **``tmux send-keys`` only queues input**, so a pane captured immediately
      after ``/clear`` can still show the *pre-existing* idle prompt. Treating
      that as "processed" would type ``/build`` into a session about to be
      cleared and the command would die with the conversation. Confirmation
      therefore requires the pane text to *change*, not merely to look idle.

    The final command (the ``/build``) is confirmed as *submitted* (the pane
    changed) rather than *finished* — the seat is expected to still be working
    when this returns.

    Args:
        plan: The ``reuse`` plan whose ``deliveries`` are typed, in order.
        runner: The command runner seam.
        sleeper: The sleep seam (injectable for tests).

    Returns:
        ``reuse`` when every command was delivered and confirmed, ``seat-busy``
        when the seat was mid-turn, or ``delivery-failed`` when a command could
        not be confirmed as processed.
    """
    session = plan.tmux_session
    topology = topology_for(plan.stream)
    # Prefer Remote Control's own status field over scraping the rendered TUI.
    # The scrape is a fallback for when RC cannot tell us (seat not registered,
    # or an ambiguous worktree match), not the primary signal.
    busy = seat_is_busy(topology, runner)
    if busy is True or (busy is None and not session_is_idle(_capture_pane(session, runner))):
        return "seat-busy"

    last = len(plan.deliveries) - 1
    for index, command in enumerate(plan.deliveries):
        before = _capture_pane(session, runner)
        # Residue from an EARLIER partial dispatch (FRE-920 left `/model sonnet`
        # unsent in the box) would otherwise be concatenated onto by the literal
        # send below and submitted as one garbage line. Only fired on observed
        # stale text — `C-u`'s semantics on this TUI are unproven, so it is never
        # sent speculatively and the healthy path is byte-identical to before.
        if _box_contents(before):
            runner(["tmux", "send-keys", "-t", exact_pane(session), "C-u"])
            before = _capture_pane(session, runner)
        # Literal text first, then Enter as a separate key — never let tmux
        # parse the command text itself as key names.
        runner(["tmux", "send-keys", "-t", exact_pane(session), "-l", command])
        runner(["tmux", "send-keys", "-t", exact_pane(session), "Enter"])
        resubmits = [0]

        def processed(
            before: str = before,
            is_last: bool = index == last,
            cmd: str = command,
            resubmits: list[int] = resubmits,
        ) -> bool:
            # The seat reporting itself busy is direct, structured evidence it
            # accepted the command and is working — stronger than any inference
            # from rendered text, and the only signal that distinguishes a
            # SUBMITTED /build from one merely echoed into the input box.
            if seat_is_busy(topology, runner) is True:
                return True
            pane = _capture_pane(session, runner)
            # FRE-923: the command still sitting in the box is positive evidence
            # its Enter was SWALLOWED — the TUI re-initializing after `/clear`
            # eats the keypress while buffering the characters. Re-press Enter
            # rather than waiting out a timeout that will never clear. Safe
            # against double-submission precisely because it requires our own
            # text to be observably unsubmitted: once it lands, the box is empty
            # and this never fires.
            if _pending_in_box(pane, cmd):
                if resubmits[0] < _MAX_ENTER_RESUBMITS:
                    resubmits[0] += 1
                    runner(["tmux", "send-keys", "-t", exact_pane(session), "Enter"])
                return False
            if is_last:
                # Never accept a bare text change here: `send-keys -l` echoes the
                # command into the input box, which changes the pane *before*
                # Enter is processed. Accepting that would report a lost /build
                # as a successful dispatch — the exact silent failure this whole
                # function exists to prevent. Busy is the only sufficient proof.
                return False
            return pane != before and session_is_idle(pane)

        if _poll_until(processed, timeout_s=_DELIVERY_TIMEOUT_S, sleeper=sleeper):
            continue

        # Non-final commands are allowed one benign exception: an idle pane whose
        # text never changed. `/clear` on an ALREADY-empty conversation redraws
        # to a byte-identical screen, so strict change-detection would condemn a
        # correctly-processed no-op. Requiring idle keeps this from masking a
        # genuinely stuck pane.
        if index != last and session_is_idle(_capture_pane(session, runner)):
            continue

        # Otherwise fail closed: never continue to the next command on an
        # unconfirmed one. Proceeding is exactly what loses the dispatch.
        return "delivery-failed"
    return "reuse"


def _preflight_worktree(worktree: str, runner: CommandRunner) -> bool:
    """Preflight a CLEAR worktree: fetch origin, then verify it is clean.

    Only the dirty case is guarded here — the fresh-branch cut is delegated to
    the worker's ``/build`` (or ``/adr``) Step 0, which owns the full safety
    gate. Never destroys uncommitted work.

    Args:
        worktree: The repo-relative worktree path.
        runner: The command runner seam.

    Returns:
        ``True`` if the worktree is clean, ``False`` if it has uncommitted
        changes (launch must abort).
    """
    runner(["git", "-C", worktree, "fetch", "--prune", "origin"])
    status = runner(["git", "-C", worktree, "status", "--porcelain"])
    return not status.stdout.strip()


def execute_plan(
    plan: LaunchPlan,
    runner: CommandRunner = subprocess_runner,
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> LaunchResult:
    """Execute a launch plan, performing side effects via the runner seam.

    **This function never terminates a seat** (FRE-913, owner-directed). It has
    exactly two ways to act:

    - ``reuse`` — type the plan's commands into a live seat. The seat's
      ``claude`` process, and with it the Remote-Control registration the
      owner's mobile view follows, is never touched.
    - ``launch``/``prepare`` — create a seat, reached only when the seat is
      ``absent``, where ``tmux new-session`` has nothing to collide with.

    Every other outcome performs no side effect at all. In particular an
    ``unhealthy`` seat is surfaced rather than reclaimed, and a seat that
    registers under the wrong Remote-Control name is left RUNNING rather than
    killed and retried.

    A CLEAR dispatch (reuse or create) preflights the worktree first and aborts
    on uncommitted changes, so no dispatch walks over the owner's working
    changes.

    Args:
        plan: The decided plan.
        runner: The command runner seam (injectable for tests).
        sleeper: The sleep seam used by the bounded polls (injectable so tests
            never wall-clock).

    Returns:
        The ``LaunchResult``.
    """
    if plan.command is None and not plan.deliveries:
        return LaunchResult(outcome=plan.outcome, card=plan.card, launched=False)

    if plan.reset_worktree and not _preflight_worktree(plan.worktree, runner):
        return LaunchResult(
            outcome="worktree-dirty",
            card=(
                f"[{plan.stream}] worktree-dirty → {plan.worktree} has uncommitted changes; "
                f"aborted launch (your working changes are untouched)"
            ),
            launched=False,
        )

    if plan.outcome == "reuse":
        outcome = deliver_to_seat(plan, runner, sleeper=sleeper)
        if outcome != "reuse":
            return LaunchResult(
                outcome=outcome,
                card=_delivery_failure_card(plan, outcome),
                launched=False,
            )
        return LaunchResult(outcome="reuse", card=plan.card, launched=True)

    assert plan.command is not None  # noqa: S101 - guarded by LaunchPlan.__post_init__

    # Give the Remote-Control name a chance to be released before claiming it.
    # This is risk REDUCTION, not proof: the agent listing is an observable
    # proxy and the allocator can still race us (this is exactly how a seat ends
    # up as ``build-41``). The verification below is the real safety net.
    _poll_until(
        lambda: not _agent_holding_name(plan.tmux_session, runner),
        timeout_s=_RC_NAME_FREE_TIMEOUT_S,
        sleeper=sleeper,
    )

    result = runner(plan.command)
    if result.returncode != 0:
        # A create only happens when the seat probed ``absent``, so the usual
        # cause of failure here is that the probe was wrong (a transient tmux
        # error read as "no session") and the seat is actually alive. Re-probe:
        # if it is live, this tick simply does nothing and the NEXT tick reuses
        # it — self-healing, and crucially not an unbounded per-tick failure
        # loop, which is what deleting the old teardown would otherwise create.
        if seat_state(topology_for(plan.stream), runner) == "live":
            return LaunchResult(
                outcome="seat-busy",
                card=(
                    f"[{plan.stream}] seat-busy → {plan.tmux_session} was probed absent but "
                    f"exists; nothing created or destroyed, will reuse it next tick"
                ),
                launched=False,
            )
        return LaunchResult(
            outcome="launch-failed",
            card=(
                f"[{plan.stream}] launch-failed → tmux new-session for {plan.tmux_session} "
                f"failed; no launch claimed"
            ),
            launched=False,
        )

    # Verify by IDENTITY, not by working directory. Matching on cwd (what
    # ``find_warm_session`` does) would happily accept a seat that registered as
    # ``build-41`` — precisely the failure that costs the owner mobile
    # visibility. We passed ``--session-id``, so the seat is exactly checkable.
    if not _poll_until(
        lambda: _seat_is_registered(plan.tmux_session, plan.session_id, runner),
        timeout_s=_RC_REGISTERED_TIMEOUT_S,
        sleeper=sleeper,
    ):
        # RC unreadable is NOT evidence of a bad registration — it is the absence
        # of evidence. Condemning a launch because ``claude agents`` could not be
        # parsed would fail a seat that started correctly and is already building.
        if _rc_agents(runner) is None:
            return LaunchResult(outcome=plan.outcome, card=plan.card, launched=True)
        return LaunchResult(
            outcome="registration-unverified",
            card=_registration_unverified_card(
                topology_for(plan.stream),
                _agent_holding_session(plan.session_id, runner),
                seed_command(topology_for(plan.stream), plan.ticket),
            ),
            # The seat IS running and WAS seeded with the ticket — only its RC
            # name is wrong. Reporting launched=False would deny the run stall
            # detection and run_complete tracking while it builds. This is a
            # visibility warning attached to a real launch, not a failed one.
            launched=True,
        )
    return LaunchResult(outcome=plan.outcome, card=plan.card, launched=True)


def _delivery_failure_card(plan: LaunchPlan, outcome: ResultOutcome) -> str:
    """Card for a reuse path that could not deliver (seat busy / unconfirmed)."""
    if outcome == "seat-busy":
        return (
            f"[{plan.stream}] seat-busy → {plan.tmux_session} is mid-turn; nothing was "
            f"delivered and the seat was not interrupted"
        )
    return (
        f"[{plan.stream}] delivery-failed → {plan.tmux_session} did not process a "
        f"delivered command; stopped before sending the rest (the seat is untouched "
        f"and still running)"
    )


def seat_is_busy(topology: StreamTopology, runner: CommandRunner) -> bool | None:
    """Whether the seat is mid-turn, from Remote Control's own status field.

    ``claude agents --json --all`` reports a per-seat ``status``
    (``idle``/``busy``). That is a **structured** signal from the tool itself,
    which is strictly better than inferring readiness by scraping the rendered
    TUI (``capture-pane`` + prompt/spinner heuristics) — the scrape has no
    supported contract, breaks whenever the TUI is restyled, and has already
    produced both false-busy (FRE-845) and false-idle readings.

    Matched by **working directory**, not by name: a seat's Remote-Control name
    can drift from its tmux session name (a seat launched as ``cc-build`` can
    register as ``build-41``), whereas one seat per worktree holds. Ambiguity is
    never guessed at — zero or multiple matches return ``None``.

    Args:
        topology: The stream's launch coordinates.
        runner: The command runner seam.

    Returns:
        ``True``/``False`` when Remote Control reports the seat's status, or
        ``None`` when it cannot be determined (caller falls back to the scrape).
    """
    agents = _rc_agents(runner)
    if agents is None:
        return None
    matches = [
        agent for agent in agents if _cwd_matches(str(agent.get("cwd", "")), topology.worktree)
    ]
    if len(matches) != 1:
        return None
    status = str(matches[0].get("status", "")).strip().lower()
    if status == "busy":
        return True
    if status == "idle":
        return False
    return None


def seat_wedge_signature(topology: StreamTopology, runner: CommandRunner) -> bool:
    """Whether a seat shows the *suspected*-wedge signature (FRE-922, CC #61568).

    The signature is Remote Control **confidently** reporting the seat ``busy``
    while its tmux pane sits at the idle input prompt. That is what an orphaned
    ``run_in_background`` bash poller looks like from the outside: the poller
    keeps RC busy, but the conversation itself is idle, so the stream's reuse
    dispatch returns ``seat-busy`` every tick and never lands.

    This is deliberately a **heuristic, not a classifier**. A single observation
    is ambiguous — a genuinely mid-turn seat whose in-progress spinner the pane
    scrape momentarily missed (``session_is_idle`` is documented best-effort and
    has produced false-idle readings) reads identically. The discriminator is
    therefore *persistence*: the orchestrator only surfaces a wedge after N
    consecutive ticks (a real turn re-renders its spinner within N ticks and
    resets the count; only a persistently-idle pane survives). The N-tick gate
    lives in the orchestrator, not here.

    RC that cannot be read cleanly (``seat_is_busy`` → ``None`` on an unreadable,
    zero-match, multi-match, or unknown-status registry) never fires the
    signature — an intentional blind spot: that path either dispatches (RC
    silent + pane idle → delivery proceeds) or is genuinely busy.

    Args:
        topology: The stream's launch coordinates.
        runner: The command runner seam.

    Returns:
        ``True`` iff RC confidently reports the seat busy AND its pane is idle.
    """
    if seat_is_busy(topology, runner) is not True:
        return False
    return session_is_idle(_capture_pane(topology.tmux_session, runner))


def _agent_holding_name(name: str, runner: CommandRunner) -> bool:
    """Whether any Remote-Control agent currently holds ``name``."""
    agents = _rc_agents(runner)
    if agents is None:
        # Unreadable registry is not evidence the name is free OR held. Report
        # "not held" so the pre-create wait does not spin out its full bound on
        # a question it cannot answer; the post-create verification is what
        # actually decides, and it handles unreadable explicitly.
        return False
    return any(str(agent.get("name", "")) == name for agent in agents)


def _seat_is_registered(name: str, session_id: str | None, runner: CommandRunner) -> bool:
    """Whether the seat we just launched holds the requested name.

    Requires BOTH the requested name and our own session id, so a stale agent
    still holding the name cannot be mistaken for the new seat (codex #3).
    """
    agents = _rc_agents(runner)
    if session_id is None or agents is None:
        return False
    return any(
        str(agent.get("name", "")) == name and str(agent.get("sessionId", "")) == session_id
        for agent in agents
    )


def _agent_holding_session(session_id: str | None, runner: CommandRunner) -> str | None:
    """The RC name our launched session actually took, if it registered at all."""
    agents = _rc_agents(runner)
    if session_id is None or agents is None:
        return None
    for agent in agents:
        if str(agent.get("sessionId", "")) == session_id:
            return str(agent.get("name", "")) or None
    return None


def _cwd_matches(cwd: str, worktree: str) -> bool:
    """Return True if an absolute session cwd corresponds to a stream worktree."""
    return os.path.normpath(cwd).endswith(os.path.normpath(worktree))


def find_warm_session(stream: str, runner: CommandRunner = subprocess_runner) -> str | None:
    """Resolve the stream's warm Remote-Control session id, if unambiguous.

    Parses ``claude agents --json --all`` and matches sessions by working
    directory. Returns a session id **only on an exact single match** — zero or
    multiple matches yield ``None`` (codex #2: never guess which session is the
    warm one).

    Args:
        stream: Dispatch stream key.
        runner: The command runner seam.

    Returns:
        The matching ``sessionId``, or ``None``.
    """
    topology = topology_for(stream)
    result = runner(["claude", "agents", "--json", "--all"])
    try:
        raw: object = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(raw, list):
        sessions: list[object] = raw
    elif isinstance(raw, dict):
        candidate = raw.get("agents") or raw.get("sessions") or []
        sessions = candidate if isinstance(candidate, list) else []
    else:
        sessions = []
    matches = [
        session
        for session in sessions
        if isinstance(session, dict)
        and _cwd_matches(str(session.get("cwd", "")), topology.worktree)
    ]
    if len(matches) == 1:
        session_id = matches[0].get("sessionId")
        return str(session_id) if session_id is not None else None
    return None


def _plan_to_json(plan: LaunchPlan) -> dict[str, object]:
    """Serialize a ``LaunchPlan`` to a JSON-safe dict."""
    return {
        "outcome": plan.outcome,
        "stream": plan.stream,
        "ticket": plan.ticket,
        "model": plan.model,
        "context": plan.context,
        "tmux_session": plan.tmux_session,
        "worktree": plan.worktree,
        "session_id": plan.session_id,
        "command": list(plan.command) if plan.command is not None else None,
        "reset_worktree": plan.reset_worktree,
        "card": plan.card,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Plans (and, with ``--execute``, performs) a stream dispatch."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--stream", required=True, help="Dispatch stream: build1, build2, adr.")
    parser.add_argument("--model", required=True, help="Model tier: opus, sonnet, haiku.")
    # Placeholder must itself be a well-formed identifier: the ticket id is
    # shape-checked in plan_launch, so a free-text default like "UNSPECIFIED"
    # would make a bare dry-run crash on its own default.
    parser.add_argument("--ticket", default="FRE-0", help="Ticket id, e.g. FRE-786.")
    parser.add_argument(
        "--keep", action="store_true", help="Treat as a context:keep ticket (warm context)."
    )
    parser.add_argument(
        "--no-auto-seed", action="store_true", help="Force the auto-seed mechanic off."
    )
    parser.add_argument(
        "--no-model-set", action="store_true", help="Force programmatic model-set off."
    )
    parser.add_argument(
        "--execute", action="store_true", help="Perform the launch (default: dry-run)."
    )
    parser.add_argument("--json", action="store_true", help="Emit the plan as JSON.")
    args = parser.parse_args(argv)

    capabilities = LauncherCapabilities(
        auto_seed=not args.no_auto_seed,
        model_set=not args.no_model_set,
    )
    warm_session_id = find_warm_session(args.stream) if (args.keep and args.execute) else None
    # Probe the seat only when actually executing: a dry-run must stay pure and
    # must not shell out to tmux.
    seat: SeatState = (
        seat_state(topology_for(args.stream), subprocess_runner) if args.execute else "absent"
    )

    try:
        plan = plan_launch(
            args.stream,
            args.ticket,
            args.model,
            context_keep=args.keep,
            capabilities=capabilities,
            warm_session_id=warm_session_id,
            seat=seat,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.execute:
        result = execute_plan(plan)
        if args.json:
            print(
                json.dumps(
                    {"outcome": result.outcome, "card": result.card, "launched": result.launched}
                )
            )
        else:
            print(result.outcome)
            print(result.card)
        return 0

    if args.json:
        print(json.dumps(_plan_to_json(plan), indent=2))
    else:
        print(plan.outcome)
        print(plan.card)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
