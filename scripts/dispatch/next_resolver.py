#!/usr/bin/env python3
"""Dispatch NEXT resolver — Linear GraphQL, dry-runnable (FRE-785, ADR-0110 T1).

Given a dispatch stream (``build1``, ``build2``, or ``adr``), returns that
stream's NEXT ticket, or none — reusing the Linear-native dispatch contract
verbatim (``.claude/skills/lifecycle-rules.md`` § Dispatch): a busy guard on
``In Progress``/``In Review``, then the head of ``Approved`` issues carrying
the stream's label, ordered by priority (``Urgent`` first, no-priority last)
then oldest-created, skipping any issue with an open (non-terminal)
"blocked by" relation.

Reads from Linear via the GraphQL API using an API key
(``AGENT_LINEAR_API_KEY``) — deliberately not the Linear MCP, which is
claude.ai-OAuth-authenticated and of uncertain availability outside a
session. Mirrors ``scripts/reconcile_board.py``'s existing Linear-API
approach (stdlib ``urllib`` only).

Callable by hand::

    python -m scripts.dispatch.next_resolver --stream build2
    python -m scripts.dispatch.next_resolver --stream build2 --json

Prints the resolved ticket identifier (or ``none``) and exits 0. Exits 1 if
no Linear API key is configured.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence

from scripts.reconcile_board import load_linear_key

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

# Busy-guard states: any issue carrying the stream's label in either of these
# states means the stream is occupied (building, or a PR at master's gate
# that could bounce back).
_OCCUPIED_STATES: frozenset[str] = frozenset({"in progress", "in review"})

# A blocked-by relation is satisfied (no longer "open") once the blocker
# reaches one of these states. Chains advance at merge, not deploy-verify, so
# `awaiting deploy` counts as terminal here — this is a distinct set from
# `reconcile_board._CLOSED_STATE_NAMES`, which is board-reconciliation
# "Done" semantics and omits `awaiting deploy`.
_TERMINAL_BLOCKER_STATES: frozenset[str] = frozenset(
    {"awaiting deploy", "done", "canceled", "cancelled", "duplicate"}
)

# Linear numeric priority (0=None, 1=Urgent, 2=High, 3=Medium, 4=Low) mapped
# to queue rank, ascending = higher priority. An explicit map (not raw
# numeric sort) because raw ascending would wrongly place None (0) before
# Urgent (1).
_PRIORITY_RANK: dict[int, int] = {1: 0, 2: 1, 3: 2, 4: 3, 0: 4}

# Linear's issue-relation type denoting a "blocks" relation. On an issue,
# `inverseRelations` yields relations where this issue is the *related* side,
# so a "blocks" inverse-relation is a blocker of this issue. The connection
# accepts no server-side `filter` argument — passing one returns HTTP 400
# (FRE-804) — so `type` is selected and filtered client-side.
_BLOCKS_RELATION_TYPE = "blocks"


@dataclasses.dataclass(frozen=True)
class Blocker:
    """A single "blocked by" relation target.

    Attributes:
        identifier: The blocking issue's identifier (e.g. ``FRE-648``).
        state: The blocking issue's current Linear state name, or ``None``
            when Linear's response omitted it (treated conservatively as
            open — never silently satisfied).
    """

    identifier: str
    state: str | None


@dataclasses.dataclass(frozen=True)
class IssueSnapshot:
    """The dispatch-relevant fields of one Linear issue.

    Attributes:
        identifier: Issue identifier (e.g. ``FRE-785``).
        state: Current Linear state name.
        priority: Linear numeric priority (0=None, 1=Urgent, 2=High,
            3=Medium, 4=Low).
        created_at: ISO-8601 creation timestamp (string-sortable).
        labels: The issue's label names.
        blocked_by: Blockers from this issue's "blocked by" relations.
    """

    identifier: str
    state: str
    priority: int
    created_at: str
    labels: frozenset[str]
    blocked_by: tuple[Blocker, ...] = ()


def stream_label(stream: str) -> str:
    """Return the Linear label name for a dispatch stream.

    Args:
        stream: The dispatch stream, e.g. ``build2``.

    Returns:
        The label name, e.g. ``stream:build2``.
    """
    return f"stream:{stream}"


def _is_occupied(issues: Sequence[IssueSnapshot], label: str) -> bool:
    """Return True if any issue carrying `label` is In Progress or In Review."""
    return any(
        label in issue.labels and issue.state.strip().lower() in _OCCUPIED_STATES
        for issue in issues
    )


def _has_open_blocker(issue: IssueSnapshot) -> bool:
    """Return True if `issue` has at least one non-terminal (or unknown-state) blocker."""
    return any(
        blocker.state is None or blocker.state.strip().lower() not in _TERMINAL_BLOCKER_STATES
        for blocker in issue.blocked_by
    )


def _queue_order(issue: IssueSnapshot) -> tuple[int, str]:
    """Sort key: priority rank ascending, then oldest-created first."""
    return (_PRIORITY_RANK.get(issue.priority, len(_PRIORITY_RANK)), issue.created_at)


def eligible_candidates(issues: Sequence[IssueSnapshot], stream: str) -> list[IssueSnapshot]:
    """Return every issue eligible to be `stream`'s NEXT, ignoring the busy guard.

    Eligible = carries the stream's label, is ``Approved``, and has no open
    "blocked by" relation — sorted priority-then-oldest-created, same order
    as `resolve_next`. Unlike `resolve_next`, this does NOT apply the busy
    guard and returns the FULL list, not just the head: master's
    advance-dispatch step needs the whole eligible set to verify the
    "exactly one Urgent-or-High ticket" invariant, and runs right after the
    merge that just freed the stream, so the busy guard doesn't apply there.

    Args:
        issues: All issues visible on the board (any state/label).
        stream: The dispatch stream, e.g. ``build1``, ``build2``, ``adr``.

    Returns:
        Eligible issues, sorted priority-then-oldest-created.
    """
    label = stream_label(stream)
    candidates = sorted(
        (i for i in issues if label in i.labels and i.state.strip().lower() == "approved"),
        key=_queue_order,
    )
    return [issue for issue in candidates if not _has_open_blocker(issue)]


def resolve_next(issues: Sequence[IssueSnapshot], stream: str) -> IssueSnapshot | None:
    """Resolve a stream's NEXT ticket from a board snapshot.

    Mirrors the Linear-native dispatch contract
    (``.claude/skills/lifecycle-rules.md`` § Dispatch): a busy guard on
    ``In Progress``/``In Review``, then the head of ``Approved`` issues
    carrying the stream's label, ordered by priority (``Urgent`` first,
    no-priority last) then oldest-created, skipping any issue with an open
    "blocked by" relation.

    Args:
        issues: All issues visible on the board (any state/label).
        stream: The dispatch stream, e.g. ``build1``, ``build2``, ``adr``.

    Returns:
        The resolved NEXT issue, or None if the stream is occupied or has no
        eligible candidate.
    """
    if _is_occupied(issues, stream_label(stream)):
        return None
    candidates = eligible_candidates(issues, stream)
    return candidates[0] if candidates else None


def fetch_board(stream: str, api_key: str) -> list[IssueSnapshot]:
    """Fetch the live board snapshot for a stream from Linear via GraphQL.

    Only issues carrying the stream's label are needed for `resolve_next`
    (busy guard + Approved head), so the query filters server-side.

    Args:
        stream: The dispatch stream, e.g. ``build2``.
        api_key: Linear personal API key.

    Returns:
        Issue snapshots for every issue carrying ``stream_label(stream)``.

    Raises:
        RuntimeError: The Linear API request failed or returned malformed data.
    """
    label = stream_label(stream)
    # `inverseRelations` takes no server-side filter (FRE-804): `type` is
    # selected on each relation node and filtered client-side below.
    query = (
        "query StreamIssues($label: String!) {"
        "  issues(filter: { labels: { name: { eq: $label } } }) {"
        "    nodes {"
        "      identifier"
        "      state { name }"
        "      priority"
        "      createdAt"
        "      labels { nodes { name } }"
        "      inverseRelations {"
        "        nodes { type issue { identifier state { name } } }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    payload = json.dumps({"query": query, "variables": {"label": label}}).encode()
    request = urllib.request.Request(  # noqa: S310 - fixed https Linear endpoint
        LINEAR_GRAPHQL_URL,
        data=payload,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # Surface the response body — GraphQL validation errors (HTTP 400)
        # carry the actionable message here, so a swallowed body must not hide
        # live-query drift again (FRE-804).
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Linear API request failed: HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Linear API request failed: {exc}") from exc
    # A 200 response can still carry GraphQL errors instead of data — surface
    # them loudly rather than silently resolving to an empty board (FRE-804).
    if data.get("errors"):
        raise RuntimeError(f"Linear API returned GraphQL errors: {data['errors']}")
    nodes = (data.get("data") or {}).get("issues", {}).get("nodes", [])
    snapshots: list[IssueSnapshot] = []
    for node in nodes:
        labels = frozenset(entry["name"] for entry in node["labels"]["nodes"])
        blocked_by = tuple(
            Blocker(
                identifier=rel["issue"]["identifier"],
                state=(rel["issue"].get("state") or {}).get("name"),
            )
            for rel in node["inverseRelations"]["nodes"]
            if rel.get("type") == _BLOCKS_RELATION_TYPE
        )
        snapshots.append(
            IssueSnapshot(
                identifier=node["identifier"],
                state=node["state"]["name"],
                priority=int(node.get("priority") or 0),
                created_at=node["createdAt"],
                labels=labels,
                blocked_by=blocked_by,
            )
        )
    return snapshots


def _issue_to_json(issue: IssueSnapshot) -> dict[str, object]:
    """Serialize an `IssueSnapshot` to a JSON-safe dict."""
    return {
        "identifier": issue.identifier,
        "state": issue.state,
        "priority": issue.priority,
        "created_at": issue.created_at,
        "labels": sorted(issue.labels),
        "blocked_by": [{"identifier": b.identifier, "state": b.state} for b in issue.blocked_by],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Prints the resolved NEXT ticket (or ``none``) for a stream."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--stream", required=True, help="Dispatch stream, e.g. build1, build2, adr."
    )
    parser.add_argument("--json", action="store_true", help="Emit the result as JSON.")
    parser.add_argument(
        "--eligible",
        action="store_true",
        help=(
            "List the full eligible set (busy guard ignored) instead of resolving a "
            "single NEXT ticket."
        ),
    )
    args = parser.parse_args(argv)

    api_key = load_linear_key()
    if not api_key:
        print("no AGENT_LINEAR_API_KEY configured", file=sys.stderr)
        return 1

    issues = fetch_board(args.stream, api_key)

    if args.eligible:
        candidates = eligible_candidates(issues, args.stream)
        if args.json:
            print(json.dumps([_issue_to_json(i) for i in candidates], indent=2))
        else:
            print("\n".join(i.identifier for i in candidates) if candidates else "none")
        return 0

    next_issue = resolve_next(issues, args.stream)

    if args.json:
        print(json.dumps(_issue_to_json(next_issue) if next_issue else None, indent=2))
    else:
        print(next_issue.identifier if next_issue else "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
