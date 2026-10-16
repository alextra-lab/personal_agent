"""The single marker identifying a Linear issue that Seshat itself created (FRE-1354).

Seshat files issues by two routes: the in-turn agent tool
(:mod:`personal_agent.tools.linear`) and the Captain's Log promotion pipeline
(:mod:`personal_agent.captains_log.promotion`). Historically each stamped its own
label — ``agent-filed`` and ``Improvement`` respectively — so no single predicate
described "a ticket Seshat created". A cap counting one leaked through the other,
silently, and ``Improvement`` is a generic word a human may also apply.

This module owns that predicate. Both creation paths apply
:data:`AGENT_AUTHORED_LABEL`. ``Improvement`` is retained on the promotion path for
continuity with ADR-0030 and the historical tickets, but it is never the counting
predicate.

The self-created ticket cap (``settings.seshat_open_ticket_cap``) throttles the
promotion pipeline, so it counts only the issues that the pipeline filed (FRE-1611).
The pipeline alone applies :data:`PIPELINE_FILED_LABEL`. The cap does not count
``agent-filed`` itself: the in-turn tool filed FRE-359..362, the owner approved them,
and they filled the whole cap of 4.
"""

from __future__ import annotations

from collections.abc import Mapping

# ``agent-filed`` rather than a new label: it already exists in the workspace, it
# already marks the historical agent-tool issues, and — unlike ``Improvement`` — it
# is not a word a human applies by accident.
AGENT_AUTHORED_LABEL = "agent-filed"

AGENT_AUTHORED_LABEL_COLOR = "#6B7280"  # neutral gray for auto-filed issues

# Applied by the promotion pipeline only. The ticket cap counts exactly this label.
PIPELINE_FILED_LABEL = "pipeline-filed"

PIPELINE_FILED_LABEL_COLOR = "#6B7280"

# Linear leaves Done and Canceled issues non-archived while their project is open, so a
# count must exclude these state types (FRE-598). ``duplicate`` is a workspace state type.
TERMINAL_STATE_TYPES: tuple[str, ...] = ("completed", "canceled", "duplicate")


def open_pipeline_issues_filter(team: str) -> Mapping[str, object]:
    """Build the Linear ``IssueFilter`` for the population that the ticket cap counts.

    The pipeline client and the in-turn tool both count with this filter, so they
    cannot disagree about what fills the cap.

    Args:
        team: Team name.

    Returns:
        The filter: open issues of ``team`` that carry :data:`PIPELINE_FILED_LABEL`.
    """
    return {
        "team": {"name": {"eq": team}},
        "state": {"type": {"nin": list(TERMINAL_STATE_TYPES)}},
        "labels": {"some": {"name": {"in": [PIPELINE_FILED_LABEL]}}},
    }


__all__ = [
    "AGENT_AUTHORED_LABEL",
    "AGENT_AUTHORED_LABEL_COLOR",
    "PIPELINE_FILED_LABEL",
    "PIPELINE_FILED_LABEL_COLOR",
    "TERMINAL_STATE_TYPES",
    "open_pipeline_issues_filter",
]
