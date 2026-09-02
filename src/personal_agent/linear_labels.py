"""The single marker identifying a Linear issue that Seshat itself created (FRE-1354).

Seshat files issues by two routes: the in-turn agent tool
(:mod:`personal_agent.tools.linear`) and the Captain's Log promotion pipeline
(:mod:`personal_agent.captains_log.promotion`). Historically each stamped its own
label — ``agent-filed`` and ``Improvement`` respectively — so no single predicate
described "a ticket Seshat created". A cap counting one leaked through the other,
silently, and ``Improvement`` is a generic word a human may also apply.

This module owns that predicate. Both creation paths apply
:data:`AGENT_AUTHORED_LABEL`, and the self-created ticket cap
(``settings.seshat_open_ticket_cap``) counts exactly the issues carrying it.
``Improvement`` is retained on the promotion path for continuity with ADR-0030 and
the historical tickets, but it is never the counting predicate.
"""

from __future__ import annotations

# ``agent-filed`` rather than a new label: it already exists in the workspace, it
# already marks the historical agent-tool issues, and — unlike ``Improvement`` — it
# is not a word a human applies by accident.
AGENT_AUTHORED_LABEL = "agent-filed"

AGENT_AUTHORED_LABEL_COLOR = "#6B7280"  # neutral gray for auto-filed issues

__all__ = ["AGENT_AUTHORED_LABEL", "AGENT_AUTHORED_LABEL_COLOR"]
