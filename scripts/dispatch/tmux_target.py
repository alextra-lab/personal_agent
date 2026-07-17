"""Exact-match tmux target construction (FRE-909).

**Why this module exists.** ``tmux`` resolves an unmatched target by *prefix*.
Our seat names are name-extensions of one another (``cc-build`` is a strict
prefix of ``cc-build2``), so a command aimed at a **dead** ``cc-build`` silently
retargets the **live** ``cc-build2``. On 2026-07-17 this destroyed a worker
mid-build: the launcher's teardown ``kill-session -t cc-build`` killed
``cc-build2`` instead, losing ~40 minutes of work on FRE-908.

Proven by controlled experiment at filing (not inferred): with live sessions
``zztest2`` and ``zzother``, ``tmux kill-session -t zztest`` — a session that
does **not** exist — returned 0 and killed ``zztest2``; ``zzother`` survived.

**The guard is not uniform**, which is why this is a module and not a one-liner
repeated at each call site:

- Session-targeting commands (``has-session``, ``kill-session``) accept the
  bare ``=name`` form.
- Pane-targeting commands (``capture-pane``, ``send-keys``, ``list-panes``)
  **reject** a bare ``=name`` with ``can't find pane: =name``. They need the
  window/pane suffix: ``=name:0.0``.

A blanket ``=name`` everywhere therefore *breaks* the watcher's send-keys rather
than fixing it. Both verified live against tmux 3.7a before this module landed.

Empirically the prefix fallback differs per command (``kill-session`` and
``capture-pane`` prefix-match; ``has-session`` did not in testing). We do not
rely on that distinction: every target goes through here, so correctness does
not depend on which commands happen to be lenient in a given tmux version.

The seat manager (``cc-sessions``) already used the exact form — this brings the
dispatch scripts up to the same convention.
"""

from __future__ import annotations

__all__ = ["exact_pane", "exact_session"]

# The seat's only window/pane. Seats are created with a single `tmux
# new-session -d`, so window 0 / pane 0 is the seat's pane.
_SEAT_PANE = "0.0"


def exact_session(session: str) -> str:
    """Return an exact-match **session** target for ``session``.

    Use for session-targeting commands (``has-session``, ``kill-session``).
    The leading ``=`` disables tmux's prefix fallback, so a name that does not
    exist resolves to nothing rather than to a name-extension of itself.

    Args:
        session: The tmux session (seat) name, e.g. ``"cc-build"``.

    Returns:
        The exact-match target, e.g. ``"=cc-build"``.
    """
    return f"={session}"


def exact_pane(session: str) -> str:
    """Return an exact-match **pane** target for ``session``'s seat pane.

    Use for pane-targeting commands (``capture-pane``, ``send-keys``,
    ``list-panes``). A bare ``=name`` is rejected by these commands with
    ``can't find pane``; the window/pane suffix is required.

    Args:
        session: The tmux session (seat) name, e.g. ``"cc-build"``.

    Returns:
        The exact-match pane target, e.g. ``"=cc-build:0.0"``.
    """
    return f"={session}:{_SEAT_PANE}"
