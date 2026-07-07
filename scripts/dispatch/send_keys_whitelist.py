#!/usr/bin/env python3
"""send-keys whitelist wrapper — non-LLM grammar parser + pane attestation (FRE-831, ADR-0113 §2).

The mechanically-enforced boundary in front of ``tmux send-keys`` for master's *own* future
actuation toward workers. ADR-0113's rationale: "an LLM master can rationalize intent into a
whitelisted command" — so the boundary cannot be a rule master polices for itself, it must be a
parser that refuses anything outside a closed grammar *before* any keystroke is sent.

**Closed grammar — ``/build <1|2|FRE-[0-9]+>`` and ``/prime-worker``, nothing else.** No ``/master``
entry: this wrapper specifically targets *LLM-driven* actuation. The existing ``gating_watcher.py``
watcher already sends ``/master <PR#>`` to ``cc-master`` directly — that trigger is emitted by a
dumb, contextless sensor (ADR-0113 §1), not an LLM, so it cannot "rationalize" anything and has no
role in this mechanism. It keeps its own crash-safety from the trigger ledger (FRE-829) unchanged.
This wrapper's job is the one actuation path an LLM genuinely drives: master deciding to poke a
worker.

**Not yet wired into any live sender.** This module is a self-contained library: grammar/pane
validation plus a ledger-integrated send. Neither ``gating_watcher.py`` nor any master skill calls
it yet — that consolidation is a follow-up ticket, kept separate so this PR stays one phase.

**Pane attestation is command-role-aware, not just membership.** ``cc-adrs`` is a real worker pane,
but its skill contract is ``/adr``, not ``/build`` (``launcher.topology_for("adr").skill_command``)
— a naive "is this pane in the known set" check would wrongly approve ``/build FRE-471`` at
``cc-adrs``. Validation checks the parsed command's own allowed pane set, derived from
``launcher.topology_for`` (never a hand-duplicated literal, so it cannot silently drift from the
real topology).

**Parser hardening.** Matched with ``str.fullmatch`` — not ``^``/``$`` (which, unlike ``fullmatch``,
matches just before a trailing newline) — over explicit ASCII character classes (``[0-9]``, never
the regex digit shorthand, which matches Unicode digits such as fullwidth or Arabic-indic lookalikes
by default). This
combination rejects embedded newlines/control characters and multi-line payloads for free: no
character class in the grammar admits them, so anything beyond the exact literal fails outright.

**``send()`` consults the trigger ledger so a send is tied to a ledger event (FRE-829)** — mirrors
``gating_watcher.run_once``'s existing ledger-before-send/consumed-after actuation block exactly,
factored out here so a future caller gets it for free. ``event_id`` is a **caller-supplied**
idempotency key; this module cannot derive one correctly itself (it does not see the caller's
dedup-relevant context, e.g. a PR's head SHA) — mirror ``gating_watcher``'s own
``<kind>:<pr>:<sha>`` pattern. A refusal (grammar, pane, or kill-switch) never touches the ledger
and never calls the runner — refused *before* any side effect (ADR-0113 AC-10).

**Exception semantics.** ``send()`` does not wrap the ``send_to_session`` call in ``try``/``except``
— this mirrors ``gating_watcher.run_once``'s existing behaviour exactly (it does not catch there
either; only ``trigger_ledger.reconcile``'s retry path does). A runner exception propagates to the
caller *after* ``mark_send_started`` has already been persisted, leaving the ledger entry in the
"started, never confirmed sent" state — exactly what a later, caller-owned ``reconcile()`` call
resolves to ``surfaced_at`` for owner intervention. ``send()`` deliberately never calls
``reconcile()`` itself: reconciliation is a per-tick, caller-owned step (as it is today in
``gating_watcher.tick()``), not a per-send one.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from collections.abc import Callable, Mapping
from typing import Literal

from scripts.dispatch import trigger_ledger
from scripts.dispatch.gating_watcher import Logger, send_to_session
from scripts.dispatch.launcher import CommandRunner, topology_for

# --- grammar -----------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class BuildCommand:
    """A validated ``/build <arg>`` invocation.

    Attributes:
        arg: ``"1"``, ``"2"`` (stream selectors), or ``"FRE-<n>"`` (an explicit ticket id) —
            exactly the real ``/build`` skill's argument grammar.
    """

    arg: str


@dataclasses.dataclass(frozen=True)
class PrimeWorkerCommand:
    """A validated bare ``/prime-worker`` invocation (no arguments)."""


ParsedCommand = BuildCommand | PrimeWorkerCommand

# ASCII-only, fullmatch()-anchored (see module docstring: no ``\d``, no ``^``/``$``).
_BUILD_RE = re.compile(r"/build (1|2|FRE-[0-9]+)")
_PRIME_WORKER_RE = re.compile(r"/prime-worker")


def parse_command(text: str) -> ParsedCommand | None:
    """Parse ``text`` against the closed grammar, or return ``None`` if it does not match.

    Args:
        text: The raw command text a caller wants to send.

    Returns:
        A ``BuildCommand``/``PrimeWorkerCommand`` on an exact grammar match, else ``None``.
    """
    build_match = _BUILD_RE.fullmatch(text)
    if build_match:
        return BuildCommand(arg=build_match.group(1))
    if _PRIME_WORKER_RE.fullmatch(text):
        return PrimeWorkerCommand()
    return None


# --- pane attestation ----------------------------------------------------------

# Derived from the real launcher topology so these can never drift from the actual worker sessions.
_BUILD_PANES: frozenset[str] = frozenset(
    topology_for(stream).tmux_session for stream in ("build1", "build2")
)
_WORKER_PANES: frozenset[str] = frozenset(
    topology_for(stream).tmux_session for stream in ("build1", "build2", "adr")
)


def attested_panes() -> frozenset[str]:
    """Return every pane the closed grammar may target, across all commands."""
    return _WORKER_PANES


def _panes_for(command: ParsedCommand) -> frozenset[str]:
    """Return the pane set a specific parsed command is allowed to target.

    ``/build`` only makes sense on a build stream (``cc-build``/``cc-build2``) — ``cc-adrs`` runs
    ``/adr``, not ``/build``. ``/prime-worker`` is generic across every worker stream.
    """
    return _BUILD_PANES if isinstance(command, BuildCommand) else _WORKER_PANES


# --- pure validation -----------------------------------------------------------

RefusalReason = Literal["ungrammatical", "unattested-pane"]


@dataclasses.dataclass(frozen=True)
class Refusal:
    """A rejected send, decided before any side effect.

    Attributes:
        reason: ``"ungrammatical"`` (outside the closed grammar) or ``"unattested-pane"`` (the
            pane is unknown, or known but wrong for this command's role).
        pane: The pane that was requested.
        text: The raw text that was requested.
    """

    reason: RefusalReason
    pane: str
    text: str


@dataclasses.dataclass(frozen=True)
class Approved:
    """A send that passed both grammar and pane attestation.

    Attributes:
        command: The parsed command.
        pane: The attested target pane.
        text: The raw text to send.
    """

    command: ParsedCommand
    pane: str
    text: str


def validate(pane: str, text: str) -> Approved | Refusal:
    """Validate a proposed send against the closed grammar and pane attestation.

    Grammar is checked first (the largest attack surface — free-form text), then pane attestation
    is command-role-aware, not mere set membership.

    Args:
        pane: The proposed target tmux session.
        text: The proposed raw command text.

    Returns:
        ``Approved`` if both checks pass, else a ``Refusal`` naming the failing check.
    """
    command = parse_command(text)
    if command is None:
        return Refusal("ungrammatical", pane, text)
    if pane not in _panes_for(command):
        return Refusal("unattested-pane", pane, text)
    return Approved(command, pane, text)


# --- ledger-integrated send ------------------------------------------------------

SendResult = Literal["sent", "busy", "absent", "refused", "ledger-duplicate", "kill-switch"]

# Refusal logging never emits an adversarial payload unbounded (CLAUDE.md: never log secrets/PII).
_REFUSAL_TEXT_LOG_LIMIT = 200


@dataclasses.dataclass(frozen=True)
class SendOutcome:
    """The result of a ``send()`` call.

    Attributes:
        result: The outcome discriminant.
        pane: The pane that was targeted.
        text: The raw text that was requested.
        reason: The refusal reason, only set when ``result == "refused"``.
    """

    result: SendResult
    pane: str
    text: str
    reason: str | None = None


def _log_preview(text: str) -> tuple[str, bool]:
    """Return a bounded preview of ``text`` and whether it was truncated."""
    if len(text) <= _REFUSAL_TEXT_LOG_LIMIT:
        return text, False
    return text[:_REFUSAL_TEXT_LOG_LIMIT], True


def send(
    pane: str,
    text: str,
    *,
    event_id: str,
    source: str,
    ticket: str,
    preconditions: Mapping[str, str],
    now: float,
    ttl_s: float,
    ledger: trigger_ledger.Ledger,
    ledger_persist: Callable[[trigger_ledger.Ledger], None],
    runner: CommandRunner,
    logger: Logger,
    trace_id: str | None = None,
    kill_switch_engaged: Callable[[], bool] = lambda: False,
) -> tuple[trigger_ledger.Ledger, SendOutcome]:
    """Validate then send, refusing anything outside grammar/pane before any side effect.

    ``event_id`` is a caller-supplied idempotency key (mirror ``gating_watcher``'s
    ``<kind>:<pr>:<sha>`` pattern) — this module trusts it and does not attempt to derive or
    validate it, since it has no visibility into the caller's own dedup-relevant context.

    Args:
        pane: The proposed target tmux session.
        text: The proposed raw command text.
        event_id: The caller's idempotency key for this actuation.
        source: A short reason for the ledger entry (e.g. ``"master-dispatch"``).
        ticket: The PR or ticket this actuation concerns.
        preconditions: A small state snapshot justifying this event (ledger audit trail).
        now: Wall-clock epoch seconds.
        ttl_s: The ledger's dedup suppression TTL for this event kind.
        ledger: The current trigger ledger.
        ledger_persist: Persists the ledger after each transition.
        runner: The command runner seam (shells ``tmux``).
        logger: Structured logger.
        trace_id: A trace id to attach to every log line; a fresh one is generated if omitted.
        kill_switch_engaged: Predicate checked first — when engaged, refuses before validation or
            any ledger read (defense-in-depth on top of a caller's own tick-level check).

    Returns:
        The (possibly updated) ledger and the ``SendOutcome``.
    """
    trace = trace_id if trace_id is not None else str(uuid.uuid4())

    if kill_switch_engaged():
        logger.warning(
            "send_keys_whitelist_blocked", trace_id=trace, reason="kill-switch", pane=pane
        )
        return ledger, SendOutcome("kill-switch", pane, text)

    decision = validate(pane, text)
    if isinstance(decision, Refusal):
        preview, truncated = _log_preview(text)
        logger.warning(
            "send_keys_whitelist_refused",
            trace_id=trace,
            reason=decision.reason,
            pane=pane,
            text=preview,
            text_truncated=truncated,
        )
        return ledger, SendOutcome("refused", pane, text, decision.reason)

    ledger, record_outcome = trigger_ledger.record_pending(
        ledger,
        event_id=event_id,
        source=source,
        target_pane=pane,
        ticket=ticket,
        command=text,
        preconditions=preconditions,
        now=now,
        ttl_s=ttl_s,
    )
    ledger_persist(ledger)
    if record_outcome == "duplicate":
        logger.warning(
            "send_keys_whitelist_ledger_duplicate", trace_id=trace, event_id=event_id, pane=pane
        )
        return ledger, SendOutcome("ledger-duplicate", pane, text)

    ledger = trigger_ledger.mark_send_started(ledger, event_id, now)
    ledger_persist(ledger)

    # No try/except here by design -- see module docstring "Exception semantics".
    outcome = send_to_session(pane, text, runner)

    if outcome == "sent":
        ledger = trigger_ledger.mark_sent(ledger, event_id, now)
        ledger_persist(ledger)
        ledger = trigger_ledger.mark_consumed(ledger, event_id, now)
        ledger_persist(ledger)
        logger.info(
            "send_keys_whitelist_sent", trace_id=trace, event_id=event_id, pane=pane, text=text
        )
    else:
        ledger = trigger_ledger.mark_consumed(ledger, event_id, now)
        ledger_persist(ledger)
        logger.warning("send_keys_whitelist_skip", trace_id=trace, reason=outcome, pane=pane)

    return ledger, SendOutcome(outcome, pane, text)
