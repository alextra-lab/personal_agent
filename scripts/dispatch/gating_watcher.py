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

Two triggers, two directions (mutually exclusive per PR per tick — a PR with a
problem is never master-ready):

- **Master ← new PR.** An open PR that is master-ready — CI green, not
  ``CONFLICTING``, and no unacked ``## Master gate — BOUNCE`` — and not already
  actuated at its current head SHA → ``/master <PR#>`` to ``cc-master``.
- **Worker ← bounce / red CI.** A PR carrying an unacked
  ``## Master gate — BOUNCE`` **or** a failed CI check on its head SHA with no
  SHA-keyed ack → ``/prime-worker`` to the owning ``cc-<stream>`` session (the
  worker's ``prime-worker`` skill then acks + self-fixes, unchanged).

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
  is not reliable enough to gate on (it kept the watcher from ever informing a
  busy master), so ``/master <id>`` is always sent and Claude Code queues it if
  master is mid-turn. Master then decides whether to act on it now or after the
  current task. The dedup store (long master TTL) still prevents re-sends.

The watcher only *actuates* the trigger; master's and worker's own gates re-read
live state and remain authoritative.

**Kill switch.** Shares the orchestrator's flag (``telemetry/dispatch.disabled``)
— its presence halts all actuation. The watcher pokes local tmux, so it does not
depend on Remote-Control reachability.

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
from typing import Literal, Protocol

import structlog

from scripts.dispatch import context_probe, trigger_ledger
from scripts.dispatch.launcher import CommandRunner, subprocess_runner, topology_for
from scripts.reconcile_board import load_linear_key

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

# The persistent master session (not a dispatch worker stream, so it has no
# launcher topology entry).
MASTER_SESSION = "cc-master"

# The exact PR-comment markers the master/worker skills use (lifecycle-rules
# § Comment channels; prime-worker Step 3.2). Kept byte-identical here — the
# em dash in the bounce marker is load-bearing.
BOUNCE_MARKER = "## Master gate — BOUNCE"
BOUNCE_ACK = "Ack: addressing master bounce"
_CI_ACK_RE = re.compile(r"Ack: addressing red CI at ([0-9a-fA-F]{7,40})")

# PR branch → ticket: worker branches are ``fre-<id>-<slug>``.
_BRANCH_RE = re.compile(r"^fre-(\d+)", re.IGNORECASE)

# Stream label → dispatch stream key (maps to a launcher tmux session).
_STREAM_FROM_LABEL: dict[str, str] = {
    "stream:build1": "build1",
    "stream:build2": "build2",
    "stream:adr": "adr",
}

# Idle/busy heuristic over ``capture-pane -p`` (best-effort, fail-safe = busy).
# Idle requires the literal input-prompt line — a bare ``❯`` caret alone on its
# line, nothing else — AND no busy marker. Real RC panes render neither
# ``│ >`` nor ``? for shortcuts`` (FRE-825: those markers never matched any
# live pane, so the watcher never injected); the caret box is rendered even
# mid-turn, so a pending permission/decision prompt or an in-progress status
# spinner both count as busy so a session mid-turn or awaiting the owner is
# never interrupted.
_IDLE_PROMPT_RE: re.Pattern[str] = re.compile(r"^\s*❯\s*$", re.MULTILINE)
# The live in-progress status line — a ``●``-prefixed line carrying an
# ellipsis followed by a parenthesised stats blurb, e.g.
# ``● Clauding… (1m 2s · ↓ 3.4k tokens · thought for 4s)`` or
# ``● Assembling and verifying system_health.ndjson… (12m 35s · ↑ 42.9k
# tokens)`` — captured live from three separate real sessions (FRE-825): the
# lead verb/description varies per tick, so the anchor is the whole-line shape
# (``●`` … ``…`` … ``(...)`` to end of line), not any one verb. Distinct from
# the completed ``✻ <verb> for Ns`` summary shown at idle, which never carries
# an ellipsis. Anchored to the full line (not a bare ``\w…\s*\(`` substring
# search) so it does not fire on an ellipsis+paren appearing inside ordinary
# prose elsewhere in the pane. The caret box is rendered even while this
# spinner is live, so this is the only reliable busy signal for a mid-turn
# pane once the tool-call-specific markers below don't match.
_BUSY_SPINNER_RE: re.Pattern[str] = re.compile(r"^\s*●\s.*…\s*\([^\n)]*\)\s*$", re.MULTILINE)
_BUSY_MARKERS: tuple[str, ...] = (
    "esc to interrupt",
    "Do you want",
    "❯ 1",
    "1. Yes",
    "No, and tell",
    "Compacting",
    "Running…",
)
# Trailing pane lines treated as the "active region" for the substring
# busy-marker check (FRE-845). ``tmux capture-pane -p`` returns the whole
# visible screen, and a completed turn's own response prose routinely
# contains phrasing that overlaps a marker word (a question, a numbered
# list, "Running the tests…"); substring-matching the markers over that
# scrollback chronically flagged an idle master as busy. The live input box,
# an in-progress spinner, and a genuine permission/decision prompt all render
# within the pane's last lines, so restricting the marker check to this
# trailing window is sufficient without parsing the box structure itself.
_ACTIVE_REGION_LINES = 30

# Dedup TTLs. master: a long self-heal re-arm; worker: a short in-flight lease
# (long enough for prime-worker to post its ack, short enough to re-arm if it
# never appears).
DEFAULT_MASTER_TTL_S: float = 21600.0  # 6 h
DEFAULT_WORKER_TTL_S: float = 900.0  # 15 min

# Context-pressure nudge (FRE-848): master checkpoint alert threshold + the
# dedup TTL for it. Reuses the master TTL -- one nudge per pressure episode,
# self-heals after 6h if master is still over threshold.
DEFAULT_CONTEXT_PRESSURE_THRESHOLD: float = 70.0
DEFAULT_CONTEXT_PRESSURE_TTL_S: float = DEFAULT_MASTER_TTL_S
_CONTEXT_PRESSURE_NUDGE = (
    "Context at {pct}% — checkpoint MASTER_PLAN + run the prime-master pre-reset gate; "
    "consider /clear at the next clean boundary."
)

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
    """

    number: int
    head_ref: str
    head_sha: str
    mergeable: str
    ci: CiStatus
    comment_bodies: tuple[str, ...]


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
    """

    kind: TriggerKind
    reason: str
    pr: int
    head_sha: str
    session: str | None
    command: str
    dedup_key: str
    ttl_s: float


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
    if not rollup:
        return "pending"
    states = [_check_state(check) for check in rollup]
    if any(state == "fail" for state in states):
        return "failure"
    if any(state == "pending" for state in states):
        return "pending"
    return "success"


def latest_bounce_unacked(comment_bodies: Sequence[str]) -> bool:
    """Return whether the latest master bounce has no worker ack after it.

    The ack *after* the latest bounce marker is the idempotency key
    (lifecycle-rules § Comment channels). Author-filtering is deliberately not
    used (master and worker may share a git identity), matching the skills.

    Args:
        comment_bodies: PR comment bodies in chronological order.

    Returns:
        ``True`` if a bounce exists and no ``Ack: addressing master bounce``
        follows the latest one.
    """
    last_bounce = -1
    last_ack = -1
    for index, body in enumerate(comment_bodies):
        if BOUNCE_MARKER in body:
            last_bounce = index
        if BOUNCE_ACK in body:
            last_ack = index
    return last_bounce != -1 and last_ack < last_bounce


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


def _active_region(pane_text: str) -> str:
    """Return the pane's trailing lines -- the live input/status area.

    Args:
        pane_text: The ``tmux capture-pane -p`` output.

    Returns:
        The last ``_ACTIVE_REGION_LINES`` lines (the whole text if shorter).
    """
    lines = pane_text.splitlines()
    return "\n".join(lines[-_ACTIVE_REGION_LINES:])


def session_is_idle(pane_text: str) -> bool:
    """Return whether a captured tmux pane looks idle at a Claude input prompt.

    Best-effort heuristic (fail-safe = not idle): idle iff the bare-caret input
    prompt line is present AND no busy marker (a tool-call-specific marker, an
    in-progress status spinner, or a pending permission/decision prompt) is
    present. The substring busy-marker check is scoped to the pane's trailing
    active region (FRE-845) — response prose further up the scrollback that
    happens to contain a marker word must not flag an otherwise-idle pane.

    Args:
        pane_text: The ``tmux capture-pane -p`` output.

    Returns:
        ``True`` only when the pane both shows the input prompt and shows no
        busy marker.
    """
    if any(marker in _active_region(pane_text) for marker in _BUSY_MARKERS):
        return False
    if _BUSY_SPINNER_RE.search(pane_text):
        return False
    return bool(_IDLE_PROMPT_RE.search(pane_text))


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
    worker_reason: str | None = None
    if latest_bounce_unacked(pr.comment_bodies):
        worker_reason = "worker-bounce"
    elif pr.ci == "failure" and not has_ci_red_ack(pr.comment_bodies, pr.head_sha):
        worker_reason = "worker-ci-red"

    if worker_reason is not None:
        key = f"worker:{pr.number}:{pr.head_sha}"
        if _suppressed(sent, key, now, worker_ttl_s):
            return None
        return Candidate("worker", worker_reason, key, worker_ttl_s)

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
        if candidate.kind == "master":
            session: str | None = MASTER_SESSION
            command = f"/master {pr.number}"
        else:
            session = session_resolver(parse_ticket_from_branch(pr.head_ref))
            command = "/prime-worker"
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
            "number,headRefName,headRefOid,mergeable,statusCheckRollup,comments",
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
    checks = [c for c in rollup if isinstance(c, dict)] if isinstance(rollup, list) else []
    return PullRequest(
        number=int(data.get("number", number)),
        head_ref=str(data.get("headRefName") or ""),
        head_sha=str(data.get("headRefOid") or ""),
        mergeable=str(data.get("mergeable") or "UNKNOWN"),
        ci=ci_status(checks),
        comment_bodies=bodies,
    )


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


def send_to_session(
    session: str, command: str, runner: CommandRunner, require_idle: bool = True
) -> Literal["sent", "absent", "busy"]:
    """Inject ``command`` into ``session`` if it exists (and, when required, idle).

    Args:
        session: The target tmux session.
        command: The command line to send (e.g. ``/master 412``).
        runner: The command runner seam (shells ``tmux``).
        require_idle: When ``True`` (default), inject only into an idle pane — a
            busy pane returns ``busy`` without sending. Used for **worker**
            triggers so a build mid-turn is never interrupted. When ``False``,
            inject regardless of pane state: Claude Code queues the keys if the
            session is mid-turn. Used for the **master** trigger — idle detection
            over ``capture-pane`` is not reliable enough to gate on (it kept the
            watcher from ever informing a busy master), so master is always poked
            with ``/master <id>`` and the owner/master decides whether to act on
            it now or after the current task.

    Returns:
        ``sent`` when the keys were injected; ``absent`` when the session does
        not exist; ``busy`` when ``require_idle`` and the pane is not idle — the
        latter two perform no injection.
    """
    if runner(["tmux", "has-session", "-t", session]).returncode != 0:
        return "absent"
    if require_idle:
        pane = runner(["tmux", "capture-pane", "-t", session, "-p"])
        if not session_is_idle(pane.stdout):
            return "busy"
    # Send the literal text, then Enter as a separate key — never let tmux parse
    # the command text as key names.
    runner(["tmux", "send-keys", "-t", session, "-l", command])
    runner(["tmux", "send-keys", "-t", session, "Enter"])
    return "sent"


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

    Returns:
        The updated dedup store.
    """
    trace_id = str(uuid.uuid4())
    if kill_switch_engaged():
        logger.warning("gating_blocked", trace_id=trace_id, reason="kill-switch")
        return state
    if ledger is None:
        ledger = {}

    def _retry_pending(entry: trigger_ledger.LedgerEntry) -> trigger_ledger.SendOutcome:
        return send_to_session(entry.target_pane, entry.command, runner)

    ledger = trigger_ledger.reconcile(
        ledger, now=now, execute_pending=_retry_pending, persist=ledger_persist, logger=logger
    )

    prs = board_fetcher()
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
        ledger, record_outcome = trigger_ledger.record_pending(
            ledger,
            event_id=trigger.dedup_key,
            source=trigger.reason,
            target_pane=trigger.session,
            ticket=str(trigger.pr),
            command=trigger.command,
            preconditions={"head_sha": trigger.head_sha},
            now=now,
            ttl_s=trigger.ttl_s,
        )
        ledger_persist(ledger)
        if record_outcome == "duplicate":
            logger.warning(
                "gating_skip", trace_id=trace_id, reason="ledger-duplicate", pr=trigger.pr
            )
            continue
        ledger = trigger_ledger.mark_send_started(ledger, trigger.dedup_key, now)
        ledger_persist(ledger)
        outcome = send_to_session(
            trigger.session, trigger.command, runner, require_idle=trigger.kind != "master"
        )
        if outcome == "sent":
            logger.info(
                "gating_send",
                trace_id=trace_id,
                pr=trigger.pr,
                session=trigger.session,
                command=trigger.command,
            )
            ledger = trigger_ledger.mark_sent(ledger, trigger.dedup_key, now)
            ledger_persist(ledger)
            state[trigger.dedup_key] = now
            persist(state)
            ledger = trigger_ledger.mark_consumed(ledger, trigger.dedup_key, now)
            ledger_persist(ledger)
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
            ledger = trigger_ledger.mark_consumed(ledger, trigger.dedup_key, now)
            ledger_persist(ledger)

    for session, pct in context_pressure(context_reader(), context_pressure_threshold):
        logger.info("context_pressure", trace_id=trace_id, session=session, pct=round(pct, 1))
        if not execute:
            continue
        key = f"ctxpressure:{session}"
        if _suppressed(state, key, now, context_pressure_ttl_s):
            continue
        command = _CONTEXT_PRESSURE_NUDGE.format(pct=round(pct))
        ledger, record_outcome = trigger_ledger.record_pending(
            ledger,
            event_id=key,
            source="context-pressure",
            target_pane=session,
            ticket=session,  # non-numeric -- prune_ledger ages it out by TTL, not open-PR closure
            command=command,
            preconditions={"pct": str(round(pct, 1))},
            now=now,
            ttl_s=context_pressure_ttl_s,
        )
        ledger_persist(ledger)
        if record_outcome == "duplicate":
            logger.warning(
                "context_pressure_skip",
                trace_id=trace_id,
                session=session,
                reason="ledger-duplicate",
            )
            continue
        ledger = trigger_ledger.mark_send_started(ledger, key, now)
        ledger_persist(ledger)
        outcome = send_to_session(session, command, runner)
        if outcome == "sent":
            logger.info(
                "context_pressure_send", trace_id=trace_id, session=session, pct=round(pct, 1)
            )
            ledger = trigger_ledger.mark_sent(ledger, key, now)
            ledger_persist(ledger)
            state[key] = now
            persist(state)
            ledger = trigger_ledger.mark_consumed(ledger, key, now)
            ledger_persist(ledger)
        else:
            logger.warning(
                "context_pressure_skip", trace_id=trace_id, session=session, reason=outcome
            )
            ledger = trigger_ledger.mark_consumed(ledger, key, now)
            ledger_persist(ledger)

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
