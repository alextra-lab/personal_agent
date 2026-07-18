"""Idle/busy heuristic over a captured tmux pane (FRE-913).

Extracted verbatim from ``gating_watcher`` so both dispatch consumers can read a
seat's readiness without an import cycle: ``gating_watcher`` imports ``launcher``
at module level, so ``launcher`` — which needs the same heuristic to know when a
seat has finished processing a delivered command — cannot import it back.

The heuristic itself is unchanged; this module is a move, not a rewrite. It is
pure text analysis over ``tmux capture-pane -p`` output, with no IO of its own,
which is what makes it trivially unit-testable from either consumer.
"""

from __future__ import annotations

import re

__all__ = ["session_is_idle"]

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


def _active_region(pane_text: str) -> str:
    """Return the trailing "active" window of a captured pane.

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
