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

from scripts.dispatch.launcher import known_streams
from scripts.reconcile_board import load_linear_key

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

# Busy-guard states: any issue carrying the stream's label in either of these
# states means the stream is occupied (building, or a PR at master's gate
# that could bounce back).
_OCCUPIED_STATES: frozenset[str] = frozenset({"in progress", "in review"})

# A blocked-by relation is satisfied (no longer "open") once the blocker
# reaches one of these states. Chains advance at merge, not deploy-verify, so
# `awaiting deploy` counts as terminal here — this is a distinct set from
# `reconcile_board._PR_EXPECTED_STATES` (board-reconciliation "a merged PR is
# expected" semantics, which also includes `verify failed`, not a dispatch
# blocker-clearing concept at all).
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

# Linear state TYPES excluded from the board fetch (FRE-976). The stream label
# is kept on a ticket forever — it is never removed at merge — so
# Done/Canceled/Duplicate tickets accumulate on the label without bound. Under
# Linear's default page cap that silently truncates the label-filtered result
# set, pushing genuinely-Approved / in-flight tickets out of the window and
# starving dispatch (observed live: build1's board was 50/50 terminal, 0
# Approved visible). `resolve_next` needs only Approved (candidates) and
# In Progress / In Review (busy guard); every completed/canceled/duplicate
# workflow state maps to one of these three TYPES, so filtering by type is the
# exhaustive way to drop the accumulators. Awaiting Deploy is Linear type
# `started` (transient — a ticket sits there only until deploy-verify → Done —
# so it never accumulates) and is deliberately KEPT.
_EXCLUDED_STATE_TYPES: tuple[str, ...] = ("completed", "canceled", "duplicate")

# Page size for the paginated board fetch. Large enough that the filtered
# (non-terminal) set is almost always a single page, but `fetch_board` loops on
# `pageInfo.hasNextPage` regardless — a silent truncation is exactly the
# FRE-976 bug, so completeness is guaranteed by construction, not assumed.
_BOARD_PAGE_SIZE: int = 250


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

    This is the chokepoint every resolver path crosses — ``eligible_candidates``,
    ``resolve_next`` and ``fetch_board`` all label through here — so the
    unknown-stream guard lives here rather than at any single caller's CLI. A
    guard on one argparse parser protects only that entry point; the
    orchestrator daemon imports these functions directly and would keep
    querying a nonexistent label, matching nothing, and reporting
    ``occupied-or-no-candidate`` forever (2026-07-18).

    Args:
        stream: The dispatch stream, e.g. ``build2``.

    Returns:
        The label name, e.g. ``stream:build2``.

    Raises:
        ValueError: The stream is not a known dispatch stream. Failing here is
            the point: an unknown stream must never be indistinguishable from a
            stream with no queued work.
    """
    if stream not in known_streams():
        raise ValueError(
            f"unknown dispatch stream: {stream!r} (known: {', '.join(known_streams())})"
        )
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


def _post_linear(query: str, variables: dict[str, object], api_key: str) -> dict[str, object]:
    """POST a GraphQL query to Linear and return the parsed JSON object.

    Handles only transport-level failures — the GraphQL ``errors`` field is left
    for the caller to interpret, because it means different things per query: a
    board fetch treats any ``errors`` as broken-query drift and raises (FRE-804),
    while a single-issue lookup treats "Entity not found" as a legitimate
    ``None`` result (FRE-976). Centralising the request/error boilerplate keeps
    those two policies in their callers without duplicating the urllib block.

    Args:
        query: The GraphQL query string.
        variables: The query variables.
        api_key: Linear personal API key.

    Returns:
        The parsed JSON response object.

    Raises:
        RuntimeError: The request failed at the transport level (HTTP, URL,
            timeout, or JSON decode), or the response was not a JSON object.
    """
    payload = json.dumps({"query": query, "variables": variables}).encode()
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
    if not isinstance(data, dict):
        raise RuntimeError(f"Linear API returned a non-object response: {data!r}")
    return data


def _as_mapping(value: object) -> dict[str, object]:
    """Narrow a parsed-JSON value to a string-keyed mapping, else an empty dict."""
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    """Narrow a parsed-JSON value to a list, else an empty list."""
    return value if isinstance(value, list) else []


def _node_to_snapshot(node: dict[str, object]) -> IssueSnapshot:
    """Parse one GraphQL issue node into an ``IssueSnapshot``.

    JSON values arrive as ``object`` and are narrowed defensively — a malformed
    node degrades to empty fields rather than raising mid-parse.

    Args:
        node: A single ``issues.nodes`` entry from the board query.

    Returns:
        The parsed snapshot, keeping only ``blocks`` inverse-relations as
        blockers (FRE-804: the connection takes no server-side filter, so
        ``type`` is filtered client-side).
    """
    labels = frozenset(
        name
        for entry in _as_list(_as_mapping(node.get("labels")).get("nodes"))
        if isinstance((name := _as_mapping(entry).get("name")), str)
    )
    blockers: list[Blocker] = []
    for rel in _as_list(_as_mapping(node.get("inverseRelations")).get("nodes")):
        rel_map = _as_mapping(rel)
        if rel_map.get("type") != _BLOCKS_RELATION_TYPE:
            continue
        issue = _as_mapping(rel_map.get("issue"))
        state_name = _as_mapping(issue.get("state")).get("name")
        blockers.append(
            Blocker(
                identifier=str(issue.get("identifier")),
                state=state_name if isinstance(state_name, str) else None,
            )
        )
    priority = node.get("priority")
    state_name = _as_mapping(node.get("state")).get("name")
    return IssueSnapshot(
        identifier=str(node.get("identifier")),
        state=state_name if isinstance(state_name, str) else "",
        priority=priority if isinstance(priority, int) else 0,
        created_at=str(node.get("createdAt")),
        labels=labels,
        blocked_by=tuple(blockers),
    )


def fetch_board(stream: str, api_key: str) -> list[IssueSnapshot]:
    """Fetch the live board snapshot for a stream from Linear via GraphQL.

    Only issues carrying the stream's label are needed for `resolve_next`
    (busy guard + Approved head), so the query filters server-side by label. It
    additionally excludes terminal state *types* (``_EXCLUDED_STATE_TYPES``) and
    paginates over `pageInfo` (FRE-976): the label is never removed at merge, so
    Done/Canceled/Duplicate tickets accumulate on it and, under Linear's default
    page cap, previously truncated the set and starved dispatch. Filtering the
    accumulators server-side plus looping until ``hasNextPage`` is false makes
    the returned board both small and provably complete.

    Args:
        stream: The dispatch stream, e.g. ``build2``.
        api_key: Linear personal API key.

    Returns:
        Issue snapshots for every non-terminal issue carrying
        ``stream_label(stream)``.

    Raises:
        RuntimeError: The Linear API request failed or returned malformed data.
    """
    label = stream_label(stream)
    # `inverseRelations` takes no server-side filter (FRE-804): `type` is
    # selected on each relation node and filtered client-side in
    # ``_node_to_snapshot``.
    query = (
        "query StreamIssues($label: String!, $after: String, $first: Int!, "
        "$excluded: [String!]!) {"
        "  issues("
        "    first: $first, after: $after,"
        "    filter: { labels: { name: { eq: $label } },"
        "              state: { type: { nin: $excluded } } }"
        "  ) {"
        "    pageInfo { hasNextPage endCursor }"
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
    snapshots: list[IssueSnapshot] = []
    after: str | None = None
    while True:
        variables: dict[str, object] = {
            "label": label,
            "after": after,
            "first": _BOARD_PAGE_SIZE,
            "excluded": list(_EXCLUDED_STATE_TYPES),
        }
        data = _post_linear(query, variables, api_key)
        # A 200 response can still carry GraphQL errors instead of data — surface
        # them loudly rather than silently resolving to an empty board (FRE-804).
        if data.get("errors"):
            raise RuntimeError(f"Linear API returned GraphQL errors: {data['errors']}")
        connection = _as_mapping(_as_mapping(data.get("data")).get("issues"))
        for node in _as_list(connection.get("nodes")):
            snapshots.append(_node_to_snapshot(_as_mapping(node)))
        page_info = _as_mapping(connection.get("pageInfo"))
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        # Defensive: a truthy `hasNextPage` with no cursor would loop forever —
        # stop rather than hang the daemon on a malformed page.
        after = cursor if isinstance(cursor, str) else None
        if not after:
            break
    return snapshots


def fetch_issue_state(identifier: str, api_key: str) -> str | None:
    """Return the current Linear state NAME of a single issue by identifier.

    A DIRECT by-identifier lookup — Linear's ``issue(id:)`` accepts the human
    identifier (e.g. ``FRE-965``) — so it is immune to the label-filtered
    board's pagination window and to label removal. This is the authoritative
    reconciliation source for whether a launched ticket is still the stream's to
    track (FRE-976): the board can omit a ticket for reasons other than terminal
    completion (its stream label removed at merge, or simply paginated out), so
    board-absence must never be read as "done". A direct lookup returns the
    ticket's true state regardless.

    Args:
        identifier: The issue identifier, e.g. ``FRE-965``.
        api_key: Linear personal API key.

    Returns:
        The state name (e.g. ``"Done"``, ``"In Progress"``), or ``None`` when
        Linear reports no such issue (deleted/archived/unknown id). ``None`` is
        deliberately inconclusive — never a completion signal.

    Raises:
        RuntimeError: The Linear API request failed at the transport level
            (HTTP/URL/timeout/decode). A caller MUST treat this as inconclusive,
            NOT as terminal — a slot is never released on a lookup failure.
    """
    query = "query IssueState($id: String!) { issue(id: $id) { identifier state { name } } }"
    data = _post_linear(query, {"id": identifier}, api_key)
    raw_issue = _as_mapping(data.get("data")).get("issue")
    if not isinstance(raw_issue, dict):
        # Not found / null — Linear returns HTTP 200 with `data.issue: null` (and
        # an `errors` entry) for an unknown id. Inconclusive, never terminal.
        return None
    state = _as_mapping(raw_issue.get("state")).get("name")
    return state if isinstance(state, str) else None


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


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser.

    Split out of ``main`` so the argument contract — notably the constrained
    ``--stream`` — is testable without running a Linear query.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--stream",
        required=True,
        choices=known_streams(),
        help="Dispatch stream. Constrained: an unknown stream must fail, not resolve to 'none'.",
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Prints the resolved NEXT ticket (or ``none``) for a stream."""
    args = _build_parser().parse_args(argv)

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
