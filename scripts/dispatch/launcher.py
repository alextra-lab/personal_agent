#!/usr/bin/env python3
"""Dispatch launch primitive — Remote Control session launcher (FRE-786, ADR-0110 T2).

Given a dispatch stream, its ticket, a model tier, and the ticket's context
flag, decide *how* to start (or refuse to start) that stream's worker session,
and — on ``--execute`` — perform the launch. Mirrors
``scripts/dispatch/next_resolver.py``'s pure/IO split: a pure planner
(``plan_launch``) produces a ``LaunchPlan`` discriminated union that is fully
unit-inspectable, and a thin IO seam (``execute_plan`` with an injectable
runner) performs the side effects.

The launcher honours the ADR-0110 context contract and graceful degradation
(§2, §4), and — critically — never *claims* a model or context state it cannot
prove:

- **CLEAR** ticket, all Remote-Control mechanics available → ``launch``: a
  fresh, correctly-modelled tmux + Remote-Control session, seeded with the
  stream's skill command.
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
import shlex
import subprocess  # noqa: S404 - launches trusted, argv-built git/tmux/claude commands
import sys
import uuid
from collections.abc import Sequence
from typing import Literal, Protocol

from scripts.dispatch.tmux_target import exact_session

# Model tiers the launcher will place on a command line. Validated so no
# free-form value ever reaches the tmux-parsed inner command (codex #4).
_MODELS: frozenset[str] = frozenset({"opus", "sonnet", "haiku"})

# Permission mode every dispatched worker seat launches in. A worker runs
# unattended and the owner may be unable to reach it (RC can drop), so a seat
# that blocks on an edit-permission prompt strands unadvanceably (FRE-911).
# ``acceptEdits`` auto-approves file edits inside the isolated worktree while
# still gating non-edit actions; worktree isolation + master's PR gate are the
# compensating controls. This is the mode a healthy seat already runs in.
_WORKER_PERMISSION_MODE: str = "acceptEdits"

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

PlanOutcome = Literal["launch", "prepare", "manual-model-required", "manual-continuation"]
ResultOutcome = Literal[
    "launch",
    "prepare",
    "manual-model-required",
    "manual-continuation",
    "worktree-dirty",
    "launch-failed",
]


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


_TOPOLOGY: dict[str, StreamTopology] = {
    "build1": StreamTopology(
        "build1", ".claude/worktrees/build", "cc-build", "/build", channel_port=8790, mode="channel"
    ),
    "build2": StreamTopology(
        "build2",
        ".claude/worktrees/build2",
        "cc-build2",
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
    channel server reads it there (failing closed if absent). When ``channels``
    is off the argv is byte-for-byte the pre-channel shape (ADR-0116 §5).

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
        "--remote-control",
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
    if channels:
        # env-prefix the per-seat port (non-secret); append the allowlist ref last.
        inner_argv = ["env", f"SESHAT_CHANNEL_PORT={topology.channel_port}", *inner_argv]
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

    Returns:
        The decided ``LaunchPlan``.

    Raises:
        ValueError: The stream or model is not recognised.
    """
    topology = topology_for(stream)
    if model not in _MODELS:
        raise ValueError(f"unknown model tier: {model!r} (expected one of {sorted(_MODELS)})")

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
    """Default runner: run an argv, capturing output, never via a shell."""
    return subprocess.run(  # noqa: S603 - argv-built from validated inputs, no shell
        list(argv), capture_output=True, text=True, check=False
    )


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


def execute_plan(plan: LaunchPlan, runner: CommandRunner = subprocess_runner) -> LaunchResult:
    """Execute a launch plan, performing side effects via the runner seam.

    Manual outcomes (``manual-model-required``/``manual-continuation``) perform
    no side effect. A CLEAR launch/prepare first preflights the worktree
    (aborting on uncommitted changes), then tears down any existing session for
    this stream's persistent tmux slot (the busy-guard guarantees the stream is
    idle here, so the slot holds only a finished dispatch), then starts the
    fresh session at the ticket's model. Without the teardown, ``tmux
    new-session -s <slot>`` collides with the persistent worker session and the
    launch is abandoned. A tmux ``new-session`` failure is reported as
    ``launch-failed``, never a claimed launch.

    Args:
        plan: The decided plan.
        runner: The command runner seam (injectable for tests).

    Returns:
        The ``LaunchResult``.
    """
    if plan.command is None:
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

    # The stream's tmux session is a persistent worker "slot" that outlives a
    # single dispatch; the busy-guard guarantees the stream is idle here, so a
    # fresh CLEAR launch must first tear down any existing (finished) session
    # for this slot — otherwise `tmux new-session -s <slot>` collides and the
    # launch is abandoned. Best-effort: a non-zero return (no such session) is
    # the benign first-launch case and is ignored.
    if plan.command[:2] == ("tmux", "new-session"):
        # EXACT-match target (FRE-909). Without the ``=`` guard tmux resolves an
        # absent session by prefix, so tearing down a dead ``cc-build`` killed
        # the LIVE ``cc-build2`` mid-build (2026-07-17 incident).
        runner(["tmux", "kill-session", "-t", exact_session(plan.tmux_session)])

    result = runner(plan.command)
    if result.returncode != 0:
        return LaunchResult(
            outcome="launch-failed",
            card=(
                f"[{plan.stream}] launch-failed → tmux new-session for {plan.tmux_session} "
                f"failed; no launch claimed"
            ),
            launched=False,
        )
    return LaunchResult(outcome=plan.outcome, card=plan.card, launched=True)


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
    parser.add_argument("--ticket", default="UNSPECIFIED", help="Ticket id, e.g. FRE-786.")
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

    try:
        plan = plan_launch(
            args.stream,
            args.ticket,
            args.model,
            context_keep=args.keep,
            capabilities=capabilities,
            warm_session_id=warm_session_id,
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
