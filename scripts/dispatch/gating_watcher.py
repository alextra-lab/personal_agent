#!/usr/bin/env python3
"""Event-driven gating watcher — send-keys triggers, zero LLM context (FRE-823).

The event-driven replacement for the polling ``/loop`` crons removed 2026-07-06
(they re-read a session's context every tick and blew the 5-min prompt-cache TTL
— an uncached-cost blowup, FRE-822). This watcher runs **outside** every Claude
Code session and holds **no model context**: it polls ``gh``/Linear/``tmux``
only, so a short poll interval is cheap and never re-reads any session's
context. It pokes the **persistent** master/worker sessions via
``tmux send-keys``; it never spawns an ephemeral ``claude -p`` (continuity is
load-bearing — FRE-822 pinned constraint).

Two triggers (mutually exclusive per PR per tick — a PR with a problem is never
master-ready):

- **Master ← ready PR.** An open PR that is CI-green, not ``CONFLICTING``, and
  not already actuated at its current head SHA → ``/master <PR#>`` to
  ``cc-master`` (master leads with "Gating PR #X").
- **Worker ← red CI.** A PR with a failed CI check on its head SHA (no SHA-keyed
  ack) → a plain ``PR #N failed CI checks - correct them`` message to the owning
  ``cc-<stream>`` seat, which self-completes the fix in-session (build skill
  § responding to a poke). Bounce is **master-direct** now (master send-keys the
  worker itself), not a watcher leg.

**Dedup — one timestamped store, TTL by kind.** ``tmux send-keys`` is
*at-least-once* delivery, so a permanent "sent" set would both suppress a needed
retry after a byte-accepted-but-unprocessed send and leave a pre-ack duplicate
window. Instead the state is ``{"sent": {"<kind>:<pr>:<sha>": <epoch>}}`` (atomic
write), recorded only on a successful send, pruned each tick, and a send is
suppressed iff ``now - last_sent < ttl(kind)``:

- *master* — a long TTL (self-heal re-arm): a clean PR has no ack channel, so
  this timestamp is the durable ledger (AC-1); the long TTL lets a genuinely
  stuck send re-nudge once instead of being suppressed forever.
- *worker* — a short TTL (transient in-flight lease): the ack markers
  prime-worker posts are the *primary* idempotency key; this lease only covers
  the send→pre-ack window.

**Injection safety.** A command is never sent into a session that does not exist
(``tmux has-session``). The **idle** guard, however, is trigger-scoped:

- **worker** triggers require idle (``session_is_idle`` over ``capture-pane``) —
  a busy worker is mid-build and must not be interrupted; a busy target is
  skipped + logged, retried next tick.
- **master** triggers are **unconditional** — idle detection over ``capture-pane``
  is not reliable enough to *gate* on (it kept the watcher from ever informing a
  busy master), so ``/master <id>`` is always sent and Claude Code queues it if
  master is mid-turn. Master then decides whether to act on it now or after the
  current task. The dedup store (long master TTL) still prevents re-sends.

**Unconfirmed delivery (FRE-939).** "Claude Code queues it" is a hope, not an
observation: a master trigger sent mid-turn was returning ``sent`` and being
booked as delivered-and-consumed, so when the keys *were* lost nothing surfaced,
nothing retried, and PR 602 sat ungated for nine hours. The pane is therefore
captured on the master path too — **as evidence, never as a gate** (the send is
still unconditional; FRE-845's dropped-dispatch regression is not reintroduced).
A busy pane yields ``queued``: keys injected, receipt unobserved. Such an entry
is deliberately **not** consumed and does **not** arm the 6 h dedup TTL, so it
stays visible to the existing unconsumed-trigger read, and ``resolve_queued_triggers``
resolves it each tick — consume it once its PR is authoritatively closed,
re-offer it into a now-idle pane, or surface it once past a bounded age. There
is no blind retry: the re-offer is idle-gated, so a still-busy target costs zero
keystrokes.

The watcher only *actuates* the trigger; master's and worker's own gates re-read
live state and remain authoritative.

**Kill switch.** Shares the orchestrator's flag (``telemetry/dispatch.disabled``)
— its presence halts all actuation. The watcher pokes local tmux, so it does not
depend on Remote-Control reachability.

**Channel-mode delivery (FRE-872, ADR-0116).** A worker trigger's target seat may
be cut over to channel-mode delivery via its per-seat ``StreamTopology.mode``
flag — the watcher then POSTs a structured PR-state payload
(``build_channel_payload``) to the seat's ``seshat-dispatch`` channel instead of
``tmux send-keys``, and the send-keys/idle-scrape path is skipped entirely for
that delivery. A failed or unconfigured channel delivery falls back to send-keys
for that event within the same tick. No seat is cut over by this module today —
every ``StreamTopology`` defaults to ``send_keys`` mode (the actual per-seat
cutover is a separate, ask-first deploy). A dependabot-authored PR is never a
**worker** candidate (``classify_pr``'s boundary guard) — the gateway
structurally cannot hand any seat an instruction whose natural completion is
pushing to a branch it does not own. It can still be a **master-ready**
candidate: that path is a pure notification (master's own gate re-reads live
state and decides), so a CI-green, mergeable dependabot PR still surfaces to
master exactly like any other PR.

Callable by hand::

    python -m scripts.dispatch.gating_watcher --once            # one dry-run tick, prints decisions
    python -m scripts.dispatch.gating_watcher --once --execute  # one real tick (sends keys)
    python -m scripts.dispatch.gating_watcher --loop --execute  # daemon loop (systemd, FRE-823)
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import ContextManager, Literal, Protocol, overload

import structlog

from scripts.dispatch import context_probe, trigger_ledger
from scripts.dispatch.launcher import (
    CommandRunner,
    stream_for_tmux_session,
    subprocess_runner,
    topology_for,
)
from scripts.dispatch.pane_state import session_is_idle
from scripts.dispatch.tmux_target import exact_pane, exact_session
from scripts.reconcile_board import _git_toplevel, load_linear_key

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

# The persistent master session (not a dispatch worker stream, so it has no
# launcher topology entry).
MASTER_SESSION = "cc-master"

# The CI-red ack marker a worker may leave on a PR (lifecycle-rules § Comment
# channels). Bounce is master-direct now, so there is no bounce marker here.
_CI_ACK_RE = re.compile(r"Ack: addressing red CI at ([0-9a-fA-F]{7,40})")

# PR branch → ticket: worker branches are ``fre-<id>-<slug>``.
_BRANCH_RE = re.compile(r"^fre-(\d+)", re.IGNORECASE)

# The GitHub App login dependabot-authored PRs carry (FRE-872, ADR-0116 AC-6).
_DEPENDABOT_LOGIN = "dependabot[bot]"

# Stream label → dispatch stream key (maps to a launcher tmux session).
_STREAM_FROM_LABEL: dict[str, str] = {
    "stream:build1": "build1",
    "stream:build2": "build2",
    "stream:adr": "adr",
}


# Dedup TTLs. master: a long self-heal re-arm; worker: a short in-flight lease
# (long enough for prime-worker to post its ack, short enough to re-arm if it
# never appears).
DEFAULT_MASTER_TTL_S: float = 21600.0  # 6 h
DEFAULT_WORKER_TTL_S: float = 900.0  # 15 min

# Context-pressure nudge (FRE-848): a master context-pressure HEADS-UP (informational),
# plus the dedup TTL. Reuses the master TTL -- one nudge per pressure episode, self-heals
# after 6h if master is still over threshold. The nudge is deliberately phrased as
# surface-to-owner, NOT as an imperative to master: the reset/checkpoint decision is the
# owner's alone (a watcher nudge must never read as an instruction to auto-run prepare-reset).
DEFAULT_CONTEXT_PRESSURE_THRESHOLD: float = 70.0
DEFAULT_CONTEXT_PRESSURE_TTL_S: float = DEFAULT_MASTER_TTL_S
_CONTEXT_PRESSURE_NUDGE = (
    "Context at {pct}% — informational, owner-gated: SURFACE this to the owner as a heads-up. "
    "Do NOT run prepare-reset / checkpoint / /clear unless the owner explicitly instructs it — "
    "the reset decision is the owner's alone, never auto-run on this nudge."
)

# Age past which a still-unconfirmed gating trigger (injected into a busy pane,
# receipt never observed — FRE-939) is surfaced to the owner, once. Same value
# and reasoning as orchestrator.DEFAULT_HELD_ESCALATION_S: a fair window for a
# gate to complete without a premature alarm, and far short of the nine hours PR
# 602 sat ungated. Age-based rather than tick-based because the ledger entry
# carries a durable timestamp, so age is the honest, cadence-independent signal.
DEFAULT_QUEUED_ESCALATION_S: float = 1800.0  # 30 min

# Poll interval for the daemon loop. Cheap — the watcher holds no LLM context.
DEFAULT_POLL_INTERVAL_S: float = 60.0

# Shared with the orchestrator's kill switch (its mere presence halts dispatch);
# kept as a literal here to avoid importing the orchestrator module (AC-4:
# the watcher's import surface stays free of any LLM-adjacent dependency).
DEFAULT_KILL_SWITCH_FILE: str = "telemetry/dispatch.disabled"

# Cap on open PRs enumerated per tick (a safety bound, never reached in practice).
_OPEN_PR_LIMIT = "50"

CiStatus = Literal["success", "failure", "pending"]
TriggerKind = Literal["master", "worker"]


class Logger(Protocol):
    """The structlog subset the watcher uses."""

    def info(self, event: str, **fields: object) -> None:
        """Emit an info event."""
        ...

    def warning(self, event: str, **fields: object) -> None:
        """Emit a warning event."""
        ...


@dataclasses.dataclass(frozen=True)
class CheckResult:
    """One ``statusCheckRollup`` entry's identity + outcome (FRE-872, ADR-0116).

    The per-check detail ``ci_status``'s aggregate discards — carried
    separately so a channel-mode delivery's payload can name which check(s)
    failed, not just the PR's overall CI state.

    Attributes:
        name: The check's name (``CheckRun.name`` or ``StatusContext.context``).
        state: The same pass/fail/pending classification ``_check_state`` uses.
        conclusion: The raw conclusion/state string (``CheckRun.conclusion`` or
            ``StatusContext.state``), empty if absent.
        details_url: A link to the check's details (``CheckRun.detailsUrl`` or
            ``StatusContext.targetUrl``), empty if absent.
    """

    name: str
    state: Literal["pass", "fail", "pending"]
    conclusion: str
    details_url: str


@dataclasses.dataclass(frozen=True)
class PullRequest:
    """The gating-relevant snapshot of one open PR (a single consistent read).

    Attributes:
        number: The PR number.
        head_ref: The head branch name (e.g. ``fre-823-...``).
        head_sha: The head commit OID (full 40-char SHA).
        mergeable: GitHub's mergeability (``MERGEABLE``/``CONFLICTING``/
            ``UNKNOWN``).
        ci: Aggregate CI status over the whole check rollup.
        comment_bodies: PR comment bodies in chronological order.
        checks: The per-check detail behind ``ci`` (FRE-872) — the structured
            channel payload's proof surface (AC-3).
        is_dependabot: Whether the PR's author is the dependabot GitHub App
            (FRE-872, ADR-0116 AC-6 boundary guard).
    """

    number: int
    head_ref: str
    head_sha: str
    mergeable: str
    ci: CiStatus
    comment_bodies: tuple[str, ...]
    checks: tuple[CheckResult, ...] = ()
    is_dependabot: bool = False


@dataclasses.dataclass(frozen=True)
class Candidate:
    """A pure trigger classification for one PR, before session routing.

    Attributes:
        kind: ``master`` or ``worker``.
        reason: A short reason (``master-ready``/``worker-bounce``/
            ``worker-ci-red``).
        key: The dedup key ``<kind>:<pr>:<sha>``.
        ttl_s: The suppression TTL for this trigger kind.
    """

    kind: TriggerKind
    reason: str
    key: str
    ttl_s: float


@dataclasses.dataclass(frozen=True)
class Trigger:
    """A fully-routed trigger: what to send, where.

    Attributes:
        kind: ``master`` or ``worker``.
        reason: A short reason.
        pr: The PR number.
        head_sha: The PR head SHA.
        session: The target tmux session, or ``None`` when a worker trigger
            could not be routed to a stream (``unroutable`` — skipped + logged).
        command: The command to inject (``/master <n>`` or ``/prime-worker``).
        dedup_key: The dedup key to record on a successful send.
        ttl_s: The suppression TTL for this trigger kind.
        mode: The target seat's delivery mode (FRE-872, ADR-0116) —
            ``"channel"`` or ``"send_keys"``. Always ``"send_keys"`` for a
            master trigger (master has no ``StreamTopology`` entry in this
            ticket) or an unroutable worker trigger.
        channel_port: The target seat's channel port, only set when
            ``mode == "channel"``.
        channel_payload: The structured PR-state payload for a channel-mode
            delivery, only set when ``mode == "channel"``.
    """

    kind: TriggerKind
    reason: str
    pr: int
    head_sha: str
    session: str | None
    command: str
    dedup_key: str
    ttl_s: float
    mode: Literal["channel", "send_keys"] = "send_keys"
    channel_port: int | None = None
    channel_payload: Mapping[str, object] | None = None


@dataclasses.dataclass(frozen=True)
class ContextReading:
    """One session's raw context usage (a ``context_probe.read_context`` output).

    Attributes:
        session: The tmux session the reading is for (e.g. ``cc-master``).
        ctx: Context-window tokens (input + cache_read + cache_creation).
        model: The model id of the last main-chain turn.
    """

    session: str
    ctx: int
    model: str


# --- pure helpers ----------------------------------------------------------


def parse_ticket_from_branch(branch: str) -> str | None:
    """Return the ``FRE-<n>`` ticket a PR branch maps to, or ``None``.

    Args:
        branch: The PR head branch name (e.g. ``fre-823-event-driven-...``).

    Returns:
        ``FRE-823`` for ``fre-823-...``; ``None`` when the branch is not a
        ``fre-<id>`` worker branch.
    """
    match = _BRANCH_RE.match(branch.strip())
    return f"FRE-{match.group(1)}" if match else None


def _check_state(check: Mapping[str, object]) -> Literal["pass", "fail", "pending"]:
    """Classify one ``statusCheckRollup`` entry as pass/fail/pending."""
    typename = str(check.get("__typename") or "")
    if typename == "StatusContext":
        state = str(check.get("state") or "").upper()
        if state == "SUCCESS":
            return "pass"
        if state in {"ERROR", "FAILURE"}:
            return "fail"
        return "pending"
    # CheckRun (the default).
    if str(check.get("status") or "").upper() != "COMPLETED":
        return "pending"
    conclusion = str(check.get("conclusion") or "").upper()
    if conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        return "pass"
    if conclusion in {
        "FAILURE",
        "TIMED_OUT",
        "CANCELLED",
        "ACTION_REQUIRED",
        "STARTUP_FAILURE",
        "STALE",
    }:
        return "fail"
    # Unknown/empty conclusion on a completed check → treat as pending, never a
    # false failure (avoids a spurious worker CI-red nudge).
    return "pending"


def _check_result(check: Mapping[str, object]) -> CheckResult:
    """Build a ``CheckResult`` from one ``statusCheckRollup`` entry (FRE-872).

    Reads name/conclusion/details-url per the same ``__typename`` branch
    ``_check_state`` already switches on — ``StatusContext`` uses
    ``context``/``state``/``targetUrl``, ``CheckRun`` uses
    ``name``/``conclusion``/``detailsUrl``.
    """
    typename = str(check.get("__typename") or "")
    if typename == "StatusContext":
        name = str(check.get("context") or "")
        conclusion = str(check.get("state") or "")
        details_url = str(check.get("targetUrl") or "")
    else:
        name = str(check.get("name") or "")
        conclusion = str(check.get("conclusion") or "")
        details_url = str(check.get("detailsUrl") or "")
    return CheckResult(
        name=name, state=_check_state(check), conclusion=conclusion, details_url=details_url
    )


def _aggregate_ci_status(states: Sequence[Literal["pass", "fail", "pending"]]) -> CiStatus:
    """Aggregate per-check states into one PR-level CI status.

    Factored out of ``ci_status`` so a caller that has already computed each
    check's state (``_fetch_pr_detail``, building ``CheckResult`` entries)
    reuses those states here instead of re-deriving them via a second
    ``_check_state`` pass over the same rollup (FRE-872).
    """
    if not states:
        return "pending"
    if any(state == "fail" for state in states):
        return "failure"
    if any(state == "pending" for state in states):
        return "pending"
    return "success"


def ci_status(rollup: Sequence[Mapping[str, object]]) -> CiStatus:
    """Aggregate a PR's ``statusCheckRollup`` into one status.

    ``failure`` if any check failed; else ``pending`` if any is incomplete; else
    ``success``. An empty rollup is ``pending`` — checks have not registered yet,
    so the PR is neither master-ready nor red. "Required" is approximated by the
    whole rollup: an over-eager CI-red nudge is harmless (prime-worker
    re-validates that a *required* check failed and stays silent otherwise).

    Args:
        rollup: The PR's ``statusCheckRollup`` entries.

    Returns:
        ``success`` / ``failure`` / ``pending``.
    """
    return _aggregate_ci_status([_check_state(check) for check in rollup])


def has_ci_red_ack(comment_bodies: Sequence[str], head_sha: str) -> bool:
    """Return whether a SHA-keyed red-CI ack exists for ``head_sha``.

    prime-worker acks a red CI with ``Ack: addressing red CI at <short-sha>``
    (the short SHA is the idempotency key). This matches an ack whose SHA token
    is a prefix of the PR's full head SHA.

    Args:
        comment_bodies: PR comment bodies (any order).
        head_sha: The PR's full head SHA.

    Returns:
        ``True`` if any comment acks the current head SHA.
    """
    head = head_sha.lower()
    for body in comment_bodies:
        for match in _CI_ACK_RE.finditer(body):
            if head.startswith(match.group(1).lower()):
                return True
    return False


def build_channel_payload(pr: PullRequest) -> dict[str, object]:
    """Build the structured, JSON-serializable channel payload (FRE-872, ADR-0116).

    The AC-3 proof surface — the payload carries the PR's *live state*
    (identity, mergeable/blocked, per-check CI results, dependabot status), not
    a pre-baked imperative string, so a channel-mode seat reasons over the
    actual failure rather than executing a generic instruction.

    Args:
        pr: The PR snapshot to encode.

    Returns:
        A JSON-serializable dict.
    """
    return {
        "pr": pr.number,
        "head_sha": pr.head_sha,
        "head_ref": pr.head_ref,
        "mergeable": pr.mergeable,
        "checks": [
            {
                "name": check.name,
                "state": check.state,
                "conclusion": check.conclusion,
                "details_url": check.details_url,
            }
            for check in pr.checks
        ],
        "dependabot": pr.is_dependabot,
    }


def session_for_labels(labels: Collection[str]) -> str | None:
    """Return the ``cc-<stream>`` tmux session for a ticket's labels, or ``None``.

    Args:
        labels: The issue's label names.

    Returns:
        The stream's tmux session (``cc-build``/``cc-build2``/``cc-adrs``), or
        ``None`` when no ``stream:*`` label is present.
    """
    for label in labels:
        stream = _STREAM_FROM_LABEL.get(label)
        if stream is not None:
            return topology_for(stream).tmux_session
    return None


def context_pressure(
    readings: Sequence[ContextReading], threshold: float
) -> list[tuple[str, float]]:
    """Return the ``(session, pct)`` pairs whose context usage is at/over ``threshold``.

    Delegates the per-model window table to ``context_probe`` so the mapping is
    defined in one place (FRE-847).

    Args:
        readings: Raw per-session context readings.
        threshold: The percent threshold (e.g. ``70.0``).

    Returns:
        ``(session, pct)`` for every reading whose ``pct >= threshold``, in
        input order.
    """
    pressures: list[tuple[str, float]] = []
    for reading in readings:
        window = context_probe.MODEL_WINDOWS.get(reading.model, context_probe.DEFAULT_WINDOW)
        pct = 100 * reading.ctx / window if window else 0.0
        if pct >= threshold:
            pressures.append((reading.session, pct))
    return pressures


def _suppressed(sent: Mapping[str, float], key: str, now: float, ttl_s: float) -> bool:
    """Return whether ``key`` was actuated within the last ``ttl_s`` seconds."""
    last = sent.get(key)
    return last is not None and (now - last) < ttl_s


def classify_pr(
    pr: PullRequest,
    *,
    now: float,
    sent: Mapping[str, float],
    master_ttl_s: float,
    worker_ttl_s: float,
) -> Candidate | None:
    """Classify a PR into a (dedup-suppressed) trigger candidate, or ``None``.

    Pure — the whole dedup decision (AC-1/2/3) is provable here without any
    session routing or IO. Worker triggers take precedence over the master
    trigger (a problem PR is never master-ready); a bounce takes precedence over
    a red CI (mirrors prime-worker Step 3.2).

    Args:
        pr: The PR snapshot.
        now: Wall-clock epoch seconds.
        sent: The dedup store (key → last-sent epoch).
        master_ttl_s: Master suppression TTL.
        worker_ttl_s: Worker suppression TTL.

    Returns:
        The ``Candidate`` to actuate, or ``None`` (no trigger, or suppressed).
    """
    # Bounce is master-direct now (master send-keys the worker itself), so the
    # only watcher-owned worker trigger is a red CI on the head SHA.
    #
    # Boundary guard (FRE-872, ADR-0116 AC-6): a dependabot-authored PR is
    # NEVER a worker candidate -- structural defense-in-depth so the gateway
    # can never hand a seat an instruction whose natural completion is "push
    # to the dependabot branch." Scoped to the worker path only: the
    # master-ready path below is a pure, harmless notification (master's own
    # gate re-reads live state and decides -- the watcher only actuates), so
    # a CI-green, mergeable dependabot PR still surfaces to master exactly as
    # any other PR does. Suppressing it too (an earlier draft did) removed a
    # pre-existing capability with no replacement notification path -- caught
    # by code review.
    if (
        pr.ci == "failure"
        and not pr.is_dependabot
        and not has_ci_red_ack(pr.comment_bodies, pr.head_sha)
    ):
        key = f"worker:{pr.number}:{pr.head_sha}"
        if _suppressed(sent, key, now, worker_ttl_s):
            return None
        return Candidate("worker", "worker-ci-red", key, worker_ttl_s)

    if pr.ci == "success" and pr.mergeable != "CONFLICTING":
        key = f"master:{pr.number}:{pr.head_sha}"
        if _suppressed(sent, key, now, master_ttl_s):
            return None
        return Candidate("master", "master-ready", key, master_ttl_s)

    return None


def decide(
    prs: Sequence[PullRequest],
    *,
    session_resolver: Callable[[str | None], str | None],
    now: float,
    sent: Mapping[str, float],
    master_ttl_s: float,
    worker_ttl_s: float,
) -> list[Trigger]:
    """Decide the routed triggers for a board snapshot (pure given the resolver).

    ``session_resolver`` is consulted **only** for worker triggers (a Linear
    lookup in production), so master-ready PRs cost no resolution.

    Args:
        prs: The open-PR snapshots.
        session_resolver: Maps a ticket id (or ``None``) to a worker session, or
            ``None`` when unroutable.
        now: Wall-clock epoch seconds.
        sent: The dedup store.
        master_ttl_s: Master suppression TTL.
        worker_ttl_s: Worker suppression TTL.

    Returns:
        The triggers to actuate this tick (session may be ``None`` → unroutable).
    """
    triggers: list[Trigger] = []
    for pr in prs:
        candidate = classify_pr(
            pr, now=now, sent=sent, master_ttl_s=master_ttl_s, worker_ttl_s=worker_ttl_s
        )
        if candidate is None:
            continue
        mode: Literal["channel", "send_keys"] = "send_keys"
        channel_port: int | None = None
        channel_payload: Mapping[str, object] | None = None
        if candidate.kind == "master":
            session: str | None = MASTER_SESSION
            command = f"/master {pr.number}"
        else:
            session = session_resolver(parse_ticket_from_branch(pr.head_ref))
            command = f"PR #{pr.number} failed CI checks - correct them"
            # Per-seat mode is owned by the gateway topology (FRE-872,
            # ADR-0116) -- resolved from the tmux session name so this stays
            # independent of session_resolver's existing str | None signature.
            stream = stream_for_tmux_session(session) if session is not None else None
            topology = topology_for(stream) if stream is not None else None
            if topology is not None and topology.mode == "channel":
                mode = "channel"
                channel_port = topology.channel_port
                channel_payload = build_channel_payload(pr)
        triggers.append(
            Trigger(
                kind=candidate.kind,
                reason=candidate.reason,
                pr=pr.number,
                head_sha=pr.head_sha,
                session=session,
                command=command,
                dedup_key=candidate.key,
                ttl_s=candidate.ttl_s,
                mode=mode,
                channel_port=channel_port,
                channel_payload=channel_payload,
            )
        )
    return triggers


# --- IO seam ---------------------------------------------------------------


def fetch_open_prs(runner: CommandRunner) -> list[PullRequest]:
    """Fetch open-PR snapshots via ``gh`` (one consistent read per PR).

    Enumerates open PRs, then reads each PR's number, head ref/SHA, mergeability,
    check rollup, and comments in a single ``gh pr view`` so a tick is internally
    consistent. A PR whose detail read fails is skipped (never crashes the tick).

    Args:
        runner: The command runner seam (shells ``gh``).

    Returns:
        The open-PR snapshots.

    Raises:
        RuntimeError: The ``gh pr list`` enumeration failed.
    """
    listing = runner(
        ["gh", "pr", "list", "--state", "open", "--json", "number", "--limit", _OPEN_PR_LIMIT]
    )
    if listing.returncode != 0:
        raise RuntimeError("gh pr list failed")
    try:
        rows: object = json.loads(listing.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh pr list returned malformed JSON: {exc}") from exc
    if not isinstance(rows, list):
        return []
    prs: list[PullRequest] = []
    for row in rows:
        if not isinstance(row, dict) or "number" not in row:
            continue
        pr = _fetch_pr_detail(int(row["number"]), runner)
        if pr is not None:
            prs.append(pr)
    return prs


def _fetch_pr_detail(number: int, runner: CommandRunner) -> PullRequest | None:
    """Read one PR's gating snapshot via ``gh pr view`` (``None`` on failure)."""
    view = runner(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--json",
            "number,headRefName,headRefOid,mergeable,statusCheckRollup,comments,author",
        ]
    )
    if view.returncode != 0:
        return None
    try:
        data: object = json.loads(view.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    raw_comments = data.get("comments")
    comments = raw_comments if isinstance(raw_comments, list) else []
    ordered = sorted(
        (c for c in comments if isinstance(c, dict)), key=lambda c: str(c.get("createdAt") or "")
    )
    bodies = tuple(str(c.get("body") or "") for c in ordered)
    rollup = data.get("statusCheckRollup")
    raw_checks = [c for c in rollup if isinstance(c, dict)] if isinstance(rollup, list) else []
    author = data.get("author")
    login = str(author.get("login") or "") if isinstance(author, dict) else ""
    # Compute each check's classification once (FRE-872) and derive the
    # aggregate from those same results, rather than a second _check_state
    # pass over raw_checks via ci_status.
    checks = tuple(_check_result(c) for c in raw_checks)
    return PullRequest(
        number=int(data.get("number", number)),
        head_ref=str(data.get("headRefName") or ""),
        head_sha=str(data.get("headRefOid") or ""),
        mergeable=str(data.get("mergeable") or "UNKNOWN"),
        ci=_aggregate_ci_status([c.state for c in checks]),
        comment_bodies=bodies,
        checks=checks,
        is_dependabot=login == _DEPENDABOT_LOGIN,
    )


def pr_is_closed(number: str, runner: CommandRunner) -> bool | None:
    """Read one PR's state authoritatively via ``gh``.

    Deliberately a *per-PR* read rather than a membership test against the tick's
    open-PR list. That list is not an authoritative inventory: ``fetch_open_prs``
    caps enumeration at ``_OPEN_PR_LIMIT`` and silently omits any PR whose detail
    read failed. Treating absence from it as closure would let one transient
    ``gh`` failure consume an unconfirmed trigger — and, since ``prune_ledger``
    evicts a consumed numeric entry whose PR is not open, destroy it permanently
    and silently. That is the FRE-939 failure mode wearing a different hat.

    Args:
        number: The PR number as a string (a ledger ``ticket``).
        runner: The command runner seam (shells ``gh``).

    Returns:
        ``True`` when the PR is definitively ``CLOSED``/``MERGED``, ``False``
        when definitively ``OPEN``, and ``None`` when the state could not be
        determined (command failure, unparseable output, unknown state) — the
        caller must treat ``None`` as "keep the entry", never as closure.
    """
    result = runner(["gh", "pr", "view", number, "--json", "state"])
    if result.returncode != 0:
        return None
    try:
        data: object = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    state = str(data.get("state") or "").upper()
    if state in ("CLOSED", "MERGED"):
        return True
    if state == "OPEN":
        return False
    return None


def fetch_issue_labels(ticket: str, api_key: str) -> frozenset[str]:
    """Fetch an issue's label names from Linear via GraphQL.

    Mirrors ``next_resolver.fetch_board``'s stdlib-``urllib`` approach.

    Args:
        ticket: The issue identifier (e.g. ``FRE-823``).
        api_key: Linear personal API key.

    Returns:
        The issue's label names.

    Raises:
        RuntimeError: The Linear request failed or returned malformed data.
    """
    query = "query IssueLabels($id: String!) { issue(id: $id) { labels { nodes { name } } } }"
    payload = json.dumps({"query": query, "variables": {"id": ticket}}).encode()
    request = urllib.request.Request(  # noqa: S310 - fixed https Linear endpoint
        LINEAR_GRAPHQL_URL,
        data=payload,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            data = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Linear label request failed: {exc}") from exc
    if data.get("errors"):
        raise RuntimeError(f"Linear returned GraphQL errors: {data['errors']}")
    issue = (data.get("data") or {}).get("issue")
    if not issue:
        return frozenset()
    return frozenset(node["name"] for node in issue["labels"]["nodes"])


@overload
def send_to_session(
    session: str,
    command: str,
    runner: CommandRunner,
    require_idle: Literal[True] = ...,
    *,
    on_queued: Callable[[], None] | None = ...,
) -> Literal["sent", "busy", "absent"]: ...


@overload
def send_to_session(
    session: str,
    command: str,
    runner: CommandRunner,
    require_idle: bool = ...,
    *,
    on_queued: Callable[[], None] | None = ...,
) -> trigger_ledger.SendOutcome: ...


def send_to_session(
    session: str,
    command: str,
    runner: CommandRunner,
    require_idle: bool = True,
    *,
    on_queued: Callable[[], None] | None = None,
) -> trigger_ledger.SendOutcome:
    """Inject ``command`` into ``session`` if it exists (and, when required, idle).

    Typed as two overloads so an **idle-gated** send can never be typed as
    ``queued``: gating on idle means a busy pane is skipped outright, so there
    is no such thing as an unconfirmed idle-gated delivery. That keeps callers
    which only ever gate on idle — ``send_keys_whitelist``, this module's own
    reconcile retry — structurally unable to receive a ``queued`` outcome and
    mishandle it by closing the entry out (which is the FRE-939 bug).

    Args:
        session: The target tmux session.
        command: The command line to send (e.g. ``/master 412``).
        runner: The command runner seam (shells ``tmux``).
        require_idle: When ``True`` (default), inject only into an idle pane — a
            busy pane returns ``busy`` without sending. Used for **worker**
            triggers so a build mid-turn is never interrupted. When ``False``,
            inject regardless of pane state: Claude Code queues the keys if the
            session is mid-turn. Used for the **master** trigger — idle detection
            over ``capture-pane`` is not reliable enough to *gate* on (it kept the
            watcher from ever informing a busy master), so master is always poked
            with ``/master <id>`` and the owner/master decides whether to act on
            it now or after the current task.
        on_queued: Called once, **after** the pane reads busy and **before** the
            keystrokes are injected, on the ``require_idle=False`` path only.
            The caller uses it to durably record the unconfirmed delivery
            (FRE-939). The ordering lives here rather than at the call site so
            it cannot be forgotten: a durable "queued" record written *after*
            injection leaves a crash window whose on-disk shape is
            indistinguishable from an ambiguous mid-send crash, which
            ``trigger_ledger.reconcile`` marks terminally ``surfaced`` —
            permanently disabling delivery for that PR.

    Returns:
        ``sent`` when the keys were injected into a pane observed **idle**;
        ``queued`` when they were injected into a **busy** pane (issued, receipt
        unobserved — ``require_idle=False`` only); ``absent`` when the session
        does not exist; ``busy`` when ``require_idle`` and the pane is not idle.
        The last two perform no injection.
    """
    # Exact-match targets throughout (FRE-909): a dead seat must resolve to
    # nothing, never to a name-extension seat (cc-build -> cc-build2), which
    # would inject this command into a DIFFERENT worker mid-build.
    if runner(["tmux", "has-session", "-t", exact_session(session)]).returncode != 0:
        return "absent"
    # The pane is now captured on BOTH paths (FRE-939). It still only *gates*
    # the require_idle path; for master it is read-only evidence, used to stop
    # booking an unobserved send as a confirmed delivery. Master delivery stays
    # unconditional — FRE-845's regression (a false-busy reading dropping the
    # dispatch outright) is not reintroduced: a false busy here costs at most a
    # duplicate poke later, never a lost one.
    pane = runner(["tmux", "capture-pane", "-t", exact_pane(session), "-p"])
    outcome: trigger_ledger.SendOutcome = "sent"
    if not session_is_idle(pane.stdout):
        if require_idle:
            return "busy"
        outcome = "queued"
        if on_queued is not None:
            on_queued()
    # Send the literal text, then Enter as a separate key — never let tmux parse
    # the command text as key names.
    runner(["tmux", "send-keys", "-t", exact_pane(session), "-l", command])
    runner(["tmux", "send-keys", "-t", exact_pane(session), "Enter"])
    return outcome


ChannelOutcome = Literal["delivered", "unreachable"]


def post_channel_event(
    port: int,
    secret: str,
    payload_json: str,
    *,
    opener: Callable[..., ContextManager[object]] = urllib.request.urlopen,
    timeout_s: float = 5.0,
) -> ChannelOutcome:
    """POST a structured event to a seat's ``seshat-dispatch`` channel (FRE-872).

    Mirrors ``fetch_issue_labels``'s stdlib-``urllib`` style — no new
    dependency. Never raises: any connection failure, timeout, or non-2xx
    response is reported as ``"unreachable"`` so the caller can fall back to
    send-keys for that event.

    Args:
        port: The seat's per-seat ``SESHAT_CHANNEL_PORT``.
        secret: The shared secret sent as the ``X-Sender`` header.
        payload_json: The JSON-encoded structured payload (see
            ``build_channel_payload``).
        opener: The HTTP opener seam (injectable for tests).
        timeout_s: The request timeout in seconds.

    Returns:
        ``"delivered"`` on a 2xx response, else ``"unreachable"``.
    """
    request = urllib.request.Request(  # noqa: S310 - fixed http://127.0.0.1 channel endpoint
        f"http://127.0.0.1:{port}/",
        data=payload_json.encode(),
        headers={"X-Sender": secret, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=timeout_s) as response:  # noqa: S310
            status = int(getattr(response, "status", 0))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return "unreachable"
    return "delivered" if 200 <= status < 300 else "unreachable"


def load_channel_secret() -> str | None:
    """Resolve the gateway-side channel shared secret (FRE-872, ADR-0116).

    Prefers ``AGENT_SESHAT_CHANNEL_SECRET``; otherwise parses ``.env`` at the
    git toplevel, mirroring ``load_linear_key``. Distinct from the seat-side
    ``SESHAT_CHANNEL_SECRET`` the Node channel process reads (FRE-871,
    unprefixed since it isn't ``personal_agent.config``-routed) — both must be
    provisioned with the identical value.
    """
    key = os.environ.get("AGENT_SESHAT_CHANNEL_SECRET")
    if key:
        return key
    root = _git_toplevel()
    if root is None:
        return None
    env_path = root / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("AGENT_SESHAT_CHANNEL_SECRET="):
            return stripped.split("=", 1)[1].strip().strip("'\"") or None
    return None


def load_state(path: Path) -> dict[str, float]:
    """Load the dedup store (key → last-sent epoch); empty if absent/invalid."""
    if not path.exists():
        return {}
    try:
        raw: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    sent = raw.get("sent")
    if not isinstance(sent, dict):
        return {}
    return {
        key: float(value)
        for key, value in sent.items()
        if isinstance(key, str) and isinstance(value, (int, float))
    }


def save_state(path: Path, sent: Mapping[str, float]) -> None:
    """Persist the dedup store atomically (temp file + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"sent": dict(sent)}, indent=2)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, path)


def _pr_of_key(key: str) -> str | None:
    """Extract the PR number component of a ``<kind>:<pr>:<sha>`` dedup key."""
    parts = key.split(":")
    return parts[1] if len(parts) >= 3 else None


def prune_state(
    sent: Mapping[str, float], *, now: float, max_ttl_s: float, open_prs: Collection[int]
) -> dict[str, float]:
    """Drop dedup entries past ``max_ttl_s`` or for PRs no longer open."""
    open_set = {str(number) for number in open_prs}
    kept: dict[str, float] = {}
    for key, last in sent.items():
        if now - last >= max_ttl_s:
            continue
        pr = _pr_of_key(key)
        if pr is not None and pr not in open_set:
            continue
        kept[key] = last
    return kept


def _kill_switch_engaged(path: Path) -> bool:
    """Return whether the shared kill-switch flag file exists (halts actuation)."""
    return path.exists()


def resolve_queued_triggers(
    ledger: trigger_ledger.Ledger,
    *,
    now: float,
    open_pr_numbers: Collection[str],
    pr_closed: Callable[[str], bool | None],
    reoffer: Callable[[trigger_ledger.LedgerEntry], trigger_ledger.SendOutcome],
    escalation_s: float,
    escalated: set[str],
    ledger_persist: Callable[[trigger_ledger.Ledger], None],
    logger: Logger,
    trace_id: str,
) -> tuple[trigger_ledger.Ledger, tuple[str, ...]]:
    """Resolve every unconfirmed (``queued``) ledger entry (FRE-939).

    An entry reaches this pass when its command was injected into a **busy**
    pane: issued, but receipt never observed. Each such entry is either closed
    out as moot, re-delivered into a now-idle pane, or — past a bounded age —
    surfaced to the owner. It is never dropped, and never blind-retried into a
    busy pane.

    Per entry, in order:

    1. **Obsolete → consume.** A PR-ticketed entry whose PR is *authoritatively*
       closed/merged is moot (master acted, or the PR went away). Absence from
       ``open_pr_numbers`` alone is never sufficient — see ``pr_is_closed``. A
       non-numeric ticket (a context-pressure nudge keyed by session) has no PR
       to close against and is never judged obsolete here.
    2. **Re-offer, idle-gated.** ``reoffer`` injects only into an idle pane, so a
       still-busy target costs **zero keystrokes**. That is what keeps this from
       becoming the send loop the ticket forbids: there is no per-tick
       re-injection and no backoff timer to mistune. It is idle-*gated*, not
       race-free — the pane can turn busy between the capture and the send-keys,
       the same narrow pre-existing window every worker delivery carries.
    3. **Escalate by age, once.** Age is measured from ``created_at``, which
       ``record_pending`` writes exactly once; nothing on the re-offer path
       rewrites it, so repeated attempts cannot reset the clock (the FRE-927
       defect, avoided by construction rather than by discipline).

    ``escalated`` is an **in-memory** one-shot latch, deliberately not persisted
    — the FRE-922/FRE-924 lesson: a persisted crossing state can be
    first-observed already past its trigger after a restart and silently lose
    the single alert forever. In memory it gives exactly-once per daemon run and
    at-least-once across restarts, which is the correct direction to fail for an
    alert. ``mark_surfaced`` is deliberately unused: it is terminal-pending and
    never auto-retried, which would kill the re-offer above.

    Args:
        ledger: The current ledger.
        now: Wall-clock epoch seconds.
        open_pr_numbers: PR numbers seen open this tick (as strings). A hit here
            short-circuits the authoritative read — the PR is definitively open.
        pr_closed: Authoritative per-PR state read; ``None`` means undetermined
            and is always treated as "keep the entry".
        reoffer: Re-attempts one entry's delivery. Must be idle-gated.
        escalation_s: Age past which a still-unconfirmed entry is surfaced.
        escalated: In-memory one-shot latch of already-surfaced event ids,
            mutated in place.
        ledger_persist: Persists the ledger after each transition.
        logger: Structured logger.
        trace_id: The tick's trace id.

    Returns:
        The updated ledger, and the event ids whose delivery was confirmed this
        pass (the caller arms its own dedup store from these).
    """
    delivered: list[str] = []
    for event_id, entry in list(ledger.items()):
        if (
            entry.queued_at is None
            or entry.sent_at is not None
            or entry.consumed_at is not None
            or entry.surfaced_at is not None
        ):
            continue

        if (
            entry.ticket.isdigit()
            and entry.ticket not in open_pr_numbers
            and pr_closed(entry.ticket) is True
        ):
            ledger = trigger_ledger.mark_consumed(ledger, event_id, now)
            ledger_persist(ledger)
            logger.info(
                "gating_queued_obsolete", trace_id=trace_id, event_id=event_id, pr=entry.ticket
            )
            continue

        outcome = reoffer(entry)
        if outcome == "sent":
            ledger = trigger_ledger.mark_sent(ledger, event_id, now)
            ledger_persist(ledger)
            ledger = trigger_ledger.mark_consumed(ledger, event_id, now)
            ledger_persist(ledger)
            delivered.append(event_id)
            logger.info(
                "gating_queued_redelivered",
                trace_id=trace_id,
                event_id=event_id,
                pr=entry.ticket,
                session=entry.target_pane,
            )
            continue

        if now - entry.created_at >= escalation_s and event_id not in escalated:
            escalated.add(event_id)
            logger.warning(
                "gating_trigger_unconfirmed_too_long",
                trace_id=trace_id,
                event_id=event_id,
                pr=entry.ticket,
                session=entry.target_pane,
                command=entry.command,
                age_s=round(now - entry.created_at, 1),
                reoffer_outcome=outcome,
            )
    return ledger, tuple(delivered)


def run_once(
    state: dict[str, float],
    *,
    now: float,
    board_fetcher: Callable[[], Sequence[PullRequest]],
    session_resolver: Callable[[str | None], str | None],
    runner: CommandRunner,
    persist: Callable[[dict[str, float]], None],
    logger: Logger,
    execute: bool,
    kill_switch_engaged: Callable[[], bool] = lambda: False,
    master_ttl_s: float = DEFAULT_MASTER_TTL_S,
    worker_ttl_s: float = DEFAULT_WORKER_TTL_S,
    ledger: trigger_ledger.Ledger | None = None,
    ledger_persist: Callable[[trigger_ledger.Ledger], None] = lambda _l: None,
    context_reader: Callable[[], Sequence[ContextReading]] = lambda: (),
    context_pressure_threshold: float = DEFAULT_CONTEXT_PRESSURE_THRESHOLD,
    context_pressure_ttl_s: float = DEFAULT_CONTEXT_PRESSURE_TTL_S,
    channel_poster: Callable[[int, str, str], ChannelOutcome] = post_channel_event,
    channel_secret: str | None = None,
    queued_escalated: set[str] | None = None,
    queued_escalation_s: float = DEFAULT_QUEUED_ESCALATION_S,
) -> dict[str, float]:
    """Run one watcher tick, mutating and returning the dedup store.

    All wall-clock and IO is injected (``now``, ``board_fetcher``,
    ``session_resolver``, ``runner``, ``persist``, ``context_reader``) so the
    tick is fully unit-testable. In dry-run (``execute=False``) it logs each
    decision and sends nothing.

    Args:
        state: The dedup store, mutated in place.
        now: Wall-clock epoch seconds.
        board_fetcher: Returns the open-PR snapshots.
        session_resolver: Maps a ticket to a worker session (worker triggers).
        runner: Command runner seam for the tmux actuation.
        persist: Persists the dedup store after a mutation.
        logger: Structured logger.
        execute: Whether to actually send keys (else dry-run, no side effects).
        kill_switch_engaged: Predicate — when engaged, all actuation is skipped.
        master_ttl_s: Master suppression TTL.
        worker_ttl_s: Worker suppression TTL.
        ledger: The durable trigger ledger (FRE-829), reconciled at the start
            of every tick and written to at the send boundary. ``None``
            allocates a fresh (empty) ledger.
        ledger_persist: Persists the ledger after each transition.
        context_reader: Returns the raw context readings to check for pressure
            (FRE-848). Defaults to none — a caller that doesn't pass this gets
            no context-pressure behavior at all.
        context_pressure_threshold: Percent threshold for the master nudge.
        context_pressure_ttl_s: Suppression TTL for the context-pressure nudge.
        channel_poster: Delivers a channel-mode worker trigger (FRE-872,
            ADR-0116). Injectable for tests.
        channel_secret: The gateway-side shared secret for channel delivery
            (``load_channel_secret()`` in production). ``None`` falls back to
            send-keys for every channel-mode trigger this tick, exactly like
            an unreachable channel.
        queued_escalated: In-memory one-shot latch of unconfirmed-trigger event
            ids already surfaced (FRE-939), held across ticks by the caller.
            ``None`` allocates a fresh set — correct for a one-shot ``--once``
            run, which surfaces at most once by definition.
        queued_escalation_s: Age past which a still-unconfirmed trigger is
            surfaced to the owner.

    Returns:
        The updated dedup store.
    """
    trace_id = str(uuid.uuid4())
    if kill_switch_engaged():
        logger.warning("gating_blocked", trace_id=trace_id, reason="kill-switch")
        return state
    # A non-optional local: the queued-record hook below mutates this through a
    # closure, and a nested `nonlocal` write invalidates any narrowing of the
    # optional parameter itself.
    tick_ledger: trigger_ledger.Ledger = {} if ledger is None else ledger
    if queued_escalated is None:
        queued_escalated = set()

    def _retry_pending(entry: trigger_ledger.LedgerEntry) -> trigger_ledger.SendOutcome:
        return send_to_session(entry.target_pane, entry.command, runner)

    if execute:
        tick_ledger = trigger_ledger.reconcile(
            tick_ledger,
            now=now,
            execute_pending=_retry_pending,
            persist=ledger_persist,
            logger=logger,
        )

    prs = board_fetcher()

    # Unconfirmed deliveries (FRE-939) resolve BEFORE this tick's decisions: a
    # re-offer that lands here arms the dedup store, so ``classify_pr`` below
    # correctly suppresses the same PR instead of racing it in one tick.
    if execute:
        tick_ledger, redelivered = resolve_queued_triggers(
            tick_ledger,
            now=now,
            open_pr_numbers={str(pr.number) for pr in prs},
            pr_closed=lambda number: pr_is_closed(number, runner),
            reoffer=_retry_pending,
            escalation_s=queued_escalation_s,
            escalated=queued_escalated,
            ledger_persist=ledger_persist,
            logger=logger,
            trace_id=trace_id,
        )
        if redelivered:
            for event_id in redelivered:
                state[event_id] = now
            persist(state)

    triggers = decide(
        prs,
        session_resolver=session_resolver,
        now=now,
        sent=state,
        master_ttl_s=master_ttl_s,
        worker_ttl_s=worker_ttl_s,
    )
    for trigger in triggers:
        logger.info(
            "gating_decision",
            trace_id=trace_id,
            kind=trigger.kind,
            reason=trigger.reason,
            pr=trigger.pr,
            session=trigger.session,
            command=trigger.command,
        )
        if trigger.session is None:
            logger.warning("gating_skip", trace_id=trace_id, reason="unroutable", pr=trigger.pr)
            continue
        if not execute:
            continue
        tick_ledger, record_outcome = trigger_ledger.record_pending(
            tick_ledger,
            event_id=trigger.dedup_key,
            source=trigger.reason,
            target_pane=trigger.session,
            ticket=str(trigger.pr),
            command=trigger.command,
            preconditions={"head_sha": trigger.head_sha},
            now=now,
            ttl_s=trigger.ttl_s,
        )
        ledger_persist(tick_ledger)
        if record_outcome == "duplicate":
            logger.warning(
                "gating_skip", trace_id=trace_id, reason="tick_ledger-duplicate", pr=trigger.pr
            )
            continue
        tick_ledger = trigger_ledger.mark_send_started(tick_ledger, trigger.dedup_key, now)
        ledger_persist(tick_ledger)
        outcome: trigger_ledger.SendOutcome | None = None
        if trigger.mode == "channel":
            if channel_secret is None:
                logger.warning(
                    "channel_secret_missing",
                    trace_id=trace_id,
                    pr=trigger.pr,
                    session=trigger.session,
                )
                channel_result: ChannelOutcome = "unreachable"
            else:
                assert trigger.channel_port is not None
                channel_result = channel_poster(
                    trigger.channel_port, channel_secret, json.dumps(trigger.channel_payload)
                )
            if channel_result == "delivered":
                # The ONLY place transport ever becomes "channel" -- called
                # exactly once, only on confirmed delivery. See
                # trigger_ledger.LedgerEntry.transport's docstring.
                tick_ledger = trigger_ledger.mark_transport(
                    tick_ledger, trigger.dedup_key, "channel"
                )
                ledger_persist(tick_ledger)
                outcome = "sent"
            elif channel_secret is not None:
                logger.warning(
                    "channel_delivery_failed",
                    trace_id=trace_id,
                    pr=trigger.pr,
                    session=trigger.session,
                )
        # Fallback (channel-down or unconfigured) and the non-channel path
        # share this single call site -- transport stays at its untouched
        # "send_keys" default in both cases; there is nothing to correct.
        if outcome is None:
            event_id = trigger.dedup_key
            target = trigger.session

            def _record_queued(event_id: str = event_id) -> None:
                # Runs BEFORE the keystrokes (see send_to_session's on_queued):
                # the durable record of an unconfirmed delivery must never trail
                # the injection it describes.
                nonlocal tick_ledger
                tick_ledger = trigger_ledger.mark_queued(tick_ledger, event_id, now)
                ledger_persist(tick_ledger)

            outcome = send_to_session(
                target,
                trigger.command,
                runner,
                require_idle=trigger.kind != "master",
                on_queued=_record_queued,
            )
        if outcome == "queued":
            # Injected into a busy pane -- delivery issued, receipt NOT observed
            # (FRE-939). Deliberately none of the "sent" bookkeeping below: no
            # mark_sent, no dedup-store write (an unconfirmed send must not
            # suppress the PR for the 6h master TTL), no mark_consumed. The
            # entry stays unconsumed so the existing surfacing read sees it and
            # resolve_queued_triggers re-offers it next tick.
            logger.warning(
                "gating_send_unconfirmed",
                trace_id=trace_id,
                pr=trigger.pr,
                session=trigger.session,
                command=trigger.command,
                reason="target-busy",
            )
        elif outcome == "sent":
            logger.info(
                "gating_send",
                trace_id=trace_id,
                pr=trigger.pr,
                session=trigger.session,
                command=trigger.command,
            )
            tick_ledger = trigger_ledger.mark_sent(tick_ledger, trigger.dedup_key, now)
            ledger_persist(tick_ledger)
            state[trigger.dedup_key] = now
            persist(state)
            tick_ledger = trigger_ledger.mark_consumed(tick_ledger, trigger.dedup_key, now)
            ledger_persist(tick_ledger)
        else:
            logger.warning(
                "gating_skip",
                trace_id=trace_id,
                reason=outcome,
                pr=trigger.pr,
                session=trigger.session,
            )
            # Abandoned, not sent -- record_pending never suppresses this on a
            # future attempt (no TTL window applies to a non-send).
            tick_ledger = trigger_ledger.mark_consumed(tick_ledger, trigger.dedup_key, now)
            ledger_persist(tick_ledger)

    for session, pct in context_pressure(context_reader(), context_pressure_threshold):
        logger.info("context_pressure", trace_id=trace_id, session=session, pct=round(pct, 1))
        if not execute:
            continue
        key = f"ctxpressure:{session}"
        if _suppressed(state, key, now, context_pressure_ttl_s):
            continue
        command = _CONTEXT_PRESSURE_NUDGE.format(pct=round(pct))
        tick_ledger, record_outcome = trigger_ledger.record_pending(
            tick_ledger,
            event_id=key,
            source="context-pressure",
            target_pane=session,
            ticket=session,  # non-numeric -- prune_ledger ages it out by TTL, not open-PR closure
            command=command,
            preconditions={"pct": str(round(pct, 1))},
            now=now,
            ttl_s=context_pressure_ttl_s,
        )
        ledger_persist(tick_ledger)
        if record_outcome == "duplicate":
            logger.warning(
                "context_pressure_skip",
                trace_id=trace_id,
                session=session,
                reason="tick_ledger-duplicate",
            )
            continue
        tick_ledger = trigger_ledger.mark_send_started(tick_ledger, key, now)
        ledger_persist(tick_ledger)
        outcome = send_to_session(session, command, runner)
        if outcome == "sent":
            logger.info(
                "context_pressure_send", trace_id=trace_id, session=session, pct=round(pct, 1)
            )
            tick_ledger = trigger_ledger.mark_sent(tick_ledger, key, now)
            ledger_persist(tick_ledger)
            state[key] = now
            persist(state)
            tick_ledger = trigger_ledger.mark_consumed(tick_ledger, key, now)
            ledger_persist(tick_ledger)
        else:
            logger.warning(
                "context_pressure_skip", trace_id=trace_id, session=session, reason=outcome
            )
            tick_ledger = trigger_ledger.mark_consumed(tick_ledger, key, now)
            ledger_persist(tick_ledger)

    pruned = prune_state(
        state,
        now=now,
        max_ttl_s=max(master_ttl_s, worker_ttl_s, context_pressure_ttl_s),
        open_prs=[pr.number for pr in prs],
    )
    if pruned != state:
        state.clear()
        state.update(pruned)
        persist(state)
    return state


def _default_state_path() -> Path:
    """Return the default dedup-store path under the repo's telemetry dir."""
    return Path("telemetry") / "gating_watcher_state.json"


def _default_ledger_path() -> Path:
    """Return the default trigger-ledger path under the repo's telemetry dir."""
    return Path("telemetry") / "trigger_ledger.json"


# How long a *consumed* ledger entry is retained for audit purposes. An
# unconsumed (pending or surfaced) entry is never pruned regardless of age
# (trigger_ledger.prune_ledger).
DEFAULT_LEDGER_RETENTION_S: float = 7 * 24 * 3600.0  # 7 days


def _context_pressure_threshold_default() -> float:
    """Resolve the ``--context-pressure-threshold`` CLI default, never raising.

    A malformed ``AGENT_CONTEXT_PRESSURE_THRESHOLD`` must not crash the whole
    watcher over one optional feature's config -- falls back to the module
    default and warns on stderr (no structlog logger exists yet this early in
    ``main()``, at argparse-build time).

    Returns:
        The parsed threshold, or ``DEFAULT_CONTEXT_PRESSURE_THRESHOLD`` if the
        env var is unset or not a valid float.
    """
    raw = os.environ.get("AGENT_CONTEXT_PRESSURE_THRESHOLD")
    if raw is None:
        return DEFAULT_CONTEXT_PRESSURE_THRESHOLD
    try:
        return float(raw)
    except ValueError:
        print(
            f"warning: AGENT_CONTEXT_PRESSURE_THRESHOLD={raw!r} is not a number; "
            f"falling back to {DEFAULT_CONTEXT_PRESSURE_THRESHOLD}",
            file=sys.stderr,
        )
        return DEFAULT_CONTEXT_PRESSURE_THRESHOLD


def _master_context_reader() -> list[ContextReading]:
    """Read master's live context usage in-process (FRE-848; no subprocess wrapper).

    Returns:
        A single-element list with master's reading, or ``[]`` if its
        transcript could not be resolved.
    """
    jsonl = context_probe.resolve_jsonl(MASTER_SESSION)
    if not jsonl or not os.path.exists(jsonl):
        return []
    ctx, model = context_probe.read_context(jsonl)
    return [ContextReading(MASTER_SESSION, ctx, model)]


def _resolver(api_key: str, logger: Logger) -> Callable[[str | None], str | None]:
    """Build the production worker-session resolver (Linear label lookup)."""

    def resolve(ticket: str | None) -> str | None:
        if not ticket:
            return None
        try:
            labels = fetch_issue_labels(ticket, api_key)
        except RuntimeError as exc:
            logger.warning("gating_label_fetch_failed", ticket=ticket, error=str(exc))
            return None
        return session_for_labels(labels)

    return resolve


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Runs one tick (``--once``) or the daemon loop (``--loop``)."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run a single tick (default).")
    mode.add_argument("--loop", action="store_true", help="Run the daemon poll loop.")
    parser.add_argument(
        "--execute", action="store_true", help="Actually send keys (default: dry-run)."
    )
    parser.add_argument(
        "--state-file", default=str(_default_state_path()), help="Path to the dedup store."
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_S,
        help="Loop poll interval seconds.",
    )
    parser.add_argument(
        "--master-ttl", type=float, default=DEFAULT_MASTER_TTL_S, help="Master dedup TTL seconds."
    )
    parser.add_argument(
        "--worker-ttl",
        type=float,
        default=DEFAULT_WORKER_TTL_S,
        help="Worker dedup lease TTL seconds.",
    )
    parser.add_argument(
        "--kill-switch-file",
        default=DEFAULT_KILL_SWITCH_FILE,
        help="Flag file whose presence halts all actuation (shared kill switch).",
    )
    parser.add_argument(
        "--ledger-file",
        default=str(_default_ledger_path()),
        help="Path to the durable trigger ledger (FRE-829).",
    )
    parser.add_argument(
        "--ledger-retention-days",
        type=float,
        default=DEFAULT_LEDGER_RETENTION_S / 86400.0,
        help="Days a consumed ledger entry is retained before pruning.",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Check the Linear key and gh availability, report, and exit (for ExecStartPre).",
    )
    parser.add_argument(
        "--context-pressure-threshold",
        type=float,
        default=_context_pressure_threshold_default(),
        help="Context-pressure percent threshold for the master nudge "
        "(env AGENT_CONTEXT_PRESSURE_THRESHOLD, default 70).",
    )
    parser.add_argument(
        "--queued-escalation-timeout",
        type=float,
        default=DEFAULT_QUEUED_ESCALATION_S,
        help="Seconds after which a still-unconfirmed gating trigger is surfaced (FRE-939).",
    )
    args = parser.parse_args(argv)

    api_key = load_linear_key()
    if not api_key:
        print("precondition unmet: AGENT_LINEAR_API_KEY is not configured", flush=True)
        return 1

    if args.preflight:
        gh_ok = subprocess_runner(["gh", "--version"]).returncode == 0
        print(f"preflight: linear key ok; gh reachable={gh_ok}", flush=True)
        return 0 if gh_ok else 1

    logger = structlog.get_logger(__name__)
    state_path = Path(args.state_file)
    ledger_path = Path(args.ledger_file)
    kill_switch_path = Path(args.kill_switch_file)
    resolver = _resolver(api_key, logger)
    ledger_retention_s = args.ledger_retention_days * 86400.0
    channel_secret = load_channel_secret()
    # One-shot unconfirmed-trigger alert latch, held across ticks for the life of
    # this daemon run and never persisted (FRE-939; the FRE-922/924 lesson —
    # a persisted crossing state can be first-observed already past its trigger
    # after a restart and silently lose the single alert forever).
    queued_escalated: set[str] = set()

    def tick() -> None:
        state = load_state(state_path)
        ledger = trigger_ledger.load_ledger(ledger_path, logger)
        fetched_prs: list[PullRequest] = []
        board_fetched = False

        def _board_fetcher() -> list[PullRequest]:
            nonlocal board_fetched
            prs = fetch_open_prs(subprocess_runner)
            fetched_prs[:] = prs
            board_fetched = True
            return prs

        run_once(
            state,
            now=time.time(),
            board_fetcher=_board_fetcher,
            session_resolver=resolver,
            runner=subprocess_runner,
            persist=lambda st: save_state(state_path, st),
            logger=logger,
            execute=args.execute,
            kill_switch_engaged=lambda: _kill_switch_engaged(kill_switch_path),
            master_ttl_s=args.master_ttl,
            worker_ttl_s=args.worker_ttl,
            ledger=ledger,
            ledger_persist=lambda lg: trigger_ledger.save_ledger(ledger_path, lg),
            context_reader=_master_context_reader,
            context_pressure_threshold=args.context_pressure_threshold,
            channel_secret=channel_secret,
            queued_escalated=queued_escalated,
            queued_escalation_s=args.queued_escalation_timeout,
        )
        if not board_fetched:
            return  # kill-switch halted the tick before the board was read
        # ledger_persist wrote every transition through run_once; reload the
        # latest state rather than pruning the stale pre-tick snapshot.
        latest_ledger = trigger_ledger.load_ledger(ledger_path, logger)
        pruned = trigger_ledger.prune_ledger(
            latest_ledger,
            now=time.time(),
            retention_s=ledger_retention_s,
            open_prs=[pr.number for pr in fetched_prs],
        )
        if pruned != latest_ledger:
            trigger_ledger.save_ledger(ledger_path, pruned)

    if args.loop:
        while True:
            try:
                tick()
            except (RuntimeError, OSError) as exc:
                logger.warning("gating_tick_failed", error=str(exc))
            time.sleep(args.interval)
    tick()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
