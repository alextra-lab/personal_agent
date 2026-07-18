#!/usr/bin/env python3
"""Deterministic, read-only delivery-board reconciler (FRE-680/FRE-861, Linear-sourced FRE-915).

Maps board *claims* to durable *evidence* and emits verdicts. No LLM. This is
a mechanical helper signal for master's judgment, not a replacement for it:
it only ever compares Linear ticket state against a repo fact (a merged PR
exists, or doesn't) — never whether a specific acceptance criterion's
*content* is proven, which requires interpretation and stays master's job
(see ``.claude/skills/lifecycle-rules.md`` § Signal trust boundary).

The claim source is **Linear ticket state**, fetched live via GraphQL
(``AGENT_LINEAR_API_KEY``). Previously (FRE-680/FRE-861) the claim source was
prose parsed out of ``MASTER_PLAN.md``; that path is retired (FRE-915) because
MASTER_PLAN is now forward-plans-only and carries no status narrative to
parse — Linear is the sole authoritative source for per-ticket state, and
always was.

Each verdict has exactly four fields: ``claim``, ``status`` (one of ``PASS`` /
``FAIL`` / ``UNVERIFIABLE``), ``evidence`` (a list of citations), and ``note``.
``UNVERIFIABLE`` (no source to check) is a first-class outcome and is never
silently treated as ``PASS``.

Callable by hand, from prime-master, and from the master post-merge step::

    python scripts/reconcile_board.py            # human-readable table
    python scripts/reconcile_board.py --json      # machine-readable verdict list

Exit code is ``1`` if any verdict is ``FAIL``, or if the Linear claim fetch
itself came back *empty* (no key, bad query — nothing was checked at all, so
this reads as a possible false pass rather than exiting clean). A non-empty
claim set that legitimately yields zero verdicts (a healthy day — nothing
drifted) is **not** a forced failure; that distinction is the fix for a
found bug where the two were conflated.

See ``.claude/skills/lifecycle-rules.md`` § Evidence contract (proof of Done).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal

Status = Literal["PASS", "FAIL", "UNVERIFIABLE"]

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
LINEAR_TEAM_KEY = "FRE"

# States where a merged PR is normal/expected: the ticket has already merged
# (Done, Awaiting Deploy) or merged and then failed post-deploy verification
# (Verify Failed — the merge already happened by definition of reaching that
# state, so a PR being present there is not drift).
_PR_EXPECTED_STATES: frozenset[str] = frozenset({"done", "awaiting deploy", "verify failed"})

# States where a merged PR's presence or absence carries no signal either
# way — a canceled/duplicate ticket may or may not have had a PR.
_PR_AMBIGUOUS_STATES: frozenset[str] = frozenset({"canceled", "cancelled", "duplicate"})


@dataclasses.dataclass(frozen=True)
class Verdict:
    """A single reconciliation verdict.

    Attributes:
        claim: The board claim being checked, in plain prose.
        status: ``PASS``, ``FAIL``, or ``UNVERIFIABLE``.
        evidence: Citations supporting the verdict (file:line, Linear state, PR #).
        note: A short human-readable explanation.
    """

    claim: str
    status: Status
    evidence: list[str]
    note: str


def verdict_for_claim(
    fre_id: str,
    state: str,
    merged_prs: Sequence[Mapping[str, object]] | None,
) -> Verdict | None:
    """Compare one ticket's Linear state against merged-PR repo evidence.

    This is the entire drift check: a ticket in a state where a merged PR is
    expected (Done / Awaiting Deploy / Verify Failed) gets one, or the claim
    is unverifiable; a ticket in an active state (Approved / In Progress / In
    Review / anything else) that already has a merged PR is drift — the
    GitHub auto-transition to Awaiting Deploy was likely missed.

    Args:
        fre_id: Ticket identifier, e.g. ``FRE-900``.
        state: The ticket's current Linear state name.
        merged_prs: Merged PRs whose branch maps to this ticket (``gh``
            results, each with at least ``number``, ``headRefName``,
            ``mergedAt``), or ``None`` if the ``gh`` lookup itself failed.

    Returns:
        A ``Verdict``, or ``None`` when there is nothing to check yet
        (ticket genuinely still in flight, or its state carries no PR
        signal either way).
    """
    normalized = state.strip().lower()
    if normalized in _PR_AMBIGUOUS_STATES:
        return None
    expected = normalized in _PR_EXPECTED_STATES
    claim = f"{fre_id} (Linear: {state}) is backed by a merged PR"

    if merged_prs is None:
        if not expected:
            return None
        return Verdict(
            claim=claim,
            status="UNVERIFIABLE",
            evidence=[f"Linear {fre_id} status={state}"],
            note="gh CLI unavailable or errored — cannot confirm a merged PR",
        )

    if merged_prs:
        evidence = [f"Linear {fre_id} status={state}"] + [
            f"PR #{pr['number']} headRef={pr['headRefName']} mergedAt={pr['mergedAt']}"
            for pr in merged_prs
        ]
        if expected:
            return Verdict(
                claim=claim,
                status="PASS",
                evidence=evidence,
                note="merged PR with branch mapping to the ticket found",
            )
        return Verdict(
            claim=f"{fre_id} (Linear: {state}) has a merged PR but is not marked Done",
            status="FAIL",
            evidence=evidence,
            note=(
                f"a merged PR already exists for {fre_id} but Linear still shows {state} — "
                "the GitHub auto-transition to Awaiting Deploy may have been missed"
            ),
        )

    if not expected:
        return None
    return Verdict(
        claim=claim,
        status="UNVERIFIABLE",
        evidence=[f"Linear {fre_id} status={state}"],
        note="no merged PR with a matching branch — may be decision-only or branch naming",
    )


PrFinder = Callable[[str], Sequence[Mapping[str, object]] | None]


def reconcile_claims_vs_repo(
    claims: Mapping[str, str],
    pr_finder: PrFinder,
) -> list[Verdict]:
    """Reconcile Linear ticket-state claims against merged-PR repo evidence.

    Args:
        claims: Ticket id -> current Linear state name. This *is* the claim
            set (FRE-915 AC1) — sourced live from Linear, never MASTER_PLAN.
        pr_finder: Callable resolving a ticket id to its merged PRs (or
            ``None`` on lookup failure). Injected so tests never touch
            ``gh`` or the network.

    Returns:
        One verdict per ticket that has something to check (see
        ``verdict_for_claim``); tickets with nothing to check yet are
        omitted, not silently passed.
    """
    verdicts: list[Verdict] = []
    for fre_id in sorted(claims):
        verdict = verdict_for_claim(fre_id, claims[fre_id], pr_finder(fre_id))
        if verdict is not None:
            verdicts.append(verdict)
    return verdicts


def _git_toplevel() -> Path | None:
    """Return the git working-tree root, or None if not in a repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return Path(out.stdout.strip())


def load_linear_key() -> str | None:
    """Resolve the Linear API key.

    Prefers the ``AGENT_LINEAR_API_KEY`` environment variable; otherwise parses
    a ``.env`` file at the git toplevel. Returns ``None`` if neither yields a
    key (callers then mark Linear-dependent verdicts ``UNVERIFIABLE``).
    """
    key = os.environ.get("AGENT_LINEAR_API_KEY")
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
        if stripped.startswith("AGENT_LINEAR_API_KEY="):
            return stripped.split("=", 1)[1].strip().strip("'\"") or None
    return None


# States where a merged PR is either expected (Done/Awaiting Deploy/Verify
# Failed) or ambiguous (Canceled/Duplicate) — together, the bucket bounded by
# recency below. Every OTHER state (Approved, In Progress, In Review, Backlog,
# Needs Approval, Triage, or any future state) falls into the unbounded
# bucket: a ticket stuck there with an already-merged PR is exactly the drift
# this reconciler exists to catch, and bounding it by recency would hide old
# stuck tickets — a real risk given the board's current size (500+ tickets,
# live-measured). Deriving the unbounded bucket as "NOT IN this list" (rather
# than hardcoding an exhaustive active-state list) means a ticket in a state
# nobody enumerated up front still gets checked, instead of silently falling
# through an allowlist.
_TERMINAL_STATE_NAMES: tuple[str, ...] = (
    "Done",
    "Awaiting Deploy",
    "Verify Failed",
    "Canceled",
    "Cancelled",
    "Duplicate",
)

_PAGE_SIZE = 100


def fetch_board_claims(api_key: str | None, since_days: int = 90) -> dict[str, str]:
    """Fetch the live Linear claim set: FRE ticket id -> current state name.

    Queries two buckets in one filter: any state NOT IN the terminal set
    (Approved/In Progress/In Review/Backlog/Needs Approval/etc.) with no
    recency bound, and states IN the terminal set (Done/Awaiting
    Deploy/Verify Failed/Canceled/Duplicate) bounded to the last
    ``since_days`` days, so the historical archive stays bounded. Paginated
    — the board currently carries 500+ tickets team-wide.

    Args:
        api_key: Linear personal API key, or ``None`` (yields an empty dict —
            callers then treat the empty claim set as a forced failure, never
            a silent pass).
        since_days: Recency window in days for the terminal-state bucket.

    Returns:
        Mapping of ticket id (e.g. ``FRE-900``) to its current Linear state
        name.
    """
    if not api_key:
        return {}
    since_iso = _iso_days_ago(since_days)
    query = (
        "query BoardClaims($terminal: [String!], $since: DateTimeOrDuration, "
        "$after: String, $pageSize: Int!) {"
        f"  issues(first: $pageSize, after: $after, filter: {{"
        f'    team: {{ key: {{ eq: "{LINEAR_TEAM_KEY}" }} }},'
        "    or: ["
        "      { state: { name: { nin: $terminal } } },"
        "      { and: [ { state: { name: { in: $terminal } } }, { updatedAt: { gt: $since } } ] }"
        "    ]"
        "  }) {"
        "    nodes { identifier state { name } }"
        "    pageInfo { hasNextPage endCursor }"
        "  }"
        "}"
    )
    claims: dict[str, str] = {}
    after: str | None = None
    while True:
        variables: dict[str, object] = {
            "terminal": list(_TERMINAL_STATE_NAMES),
            "since": since_iso,
            "after": after,
            "pageSize": _PAGE_SIZE,
        }
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
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return claims
        if data.get("errors"):
            return claims
        issues = (data.get("data") or {}).get("issues") or {}
        for node in issues.get("nodes", []):
            identifier = node.get("identifier")
            state = (node.get("state") or {}).get("name")
            if identifier and state:
                claims[identifier] = state
        page_info = issues.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            break
    return claims


def _iso_days_ago(days: int) -> str:
    """Return an ISO-8601 UTC timestamp ``days`` before the current time."""
    import datetime

    return (
        (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# Deliberately generous, not a recency bound: fetching every merged PR the
# repo has ever had is cheap (live-measured: ~570 total, ~4s at limit=10000)
# and correctness-critical — an old ticket stuck in an active state (see
# `_TERMINAL_STATE_NAMES`) needs its (possibly old) merged PR to still be
# found, or the exact drift this reconciler exists to catch goes silently
# unnoticed (a prior version capped this at 500 and was found, live, to hide
# older matches).
_MERGED_PR_FETCH_LIMIT = 5000


def _fetch_merged_prs() -> list[dict[str, object]] | None:
    """Fetch merged PRs once, via a single ``gh`` call.

    One call regardless of claim-set size — the board can carry 100+ active
    tickets, and a naive per-ticket ``gh pr list --search`` (the original
    design) means 100+ sequential subprocess calls, which was measured to
    hang past a 2-minute budget against the live board. ``gh`` has no
    server-side branch-name filter, so filtering per ticket happens against
    this one fetched batch instead, in ``_make_pr_finder``.

    Returns:
        The merged PRs (each with at least ``number``, ``headRefName``,
        ``mergedAt``), or ``None`` if ``gh`` is unavailable or errors.
    """
    try:
        out = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--json",
                "number,headRefName,mergedAt",
                "--limit",
                str(_MERGED_PR_FETCH_LIMIT),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        prs: list[dict[str, object]] = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return prs


# Anchors a ticket id to a whole branch-name segment: matches at the start of
# the branch (or right after a "/", e.g. "docs/fre-893-...") and requires the
# character after the id to be a non-digit (or end of string). Plain
# substring containment (the original implementation) let a shorter id false-
# match a longer one sharing its numeric prefix — "fre-9" is a substring of
# "fre-900-...", so FRE-9 would wrongly pick up FRE-900's PR.
def _branch_matches_ticket(head_ref: str, fre_id: str) -> bool:
    """Return whether ``head_ref`` is a branch for ``fre_id``, not merely mentions it.

    Args:
        head_ref: A PR's ``headRefName``, e.g. ``fre-900-fix-thing`` or
            ``docs/fre-893-redo-done``.
        fre_id: Ticket identifier, e.g. ``FRE-900``.

    Returns:
        ``True`` if ``fre_id`` appears as its own branch-name segment.
    """
    pattern = re.compile(rf"(?:^|/){re.escape(fre_id.lower())}(?:-|$)")
    return bool(pattern.search(head_ref.lower()))


def _make_pr_finder(merged_prs: list[dict[str, object]] | None) -> PrFinder:
    """Build a ``PrFinder`` that filters one pre-fetched merged-PR batch per ticket.

    Args:
        merged_prs: Result of ``_fetch_merged_prs()`` — ``None`` propagates
            as "gh unavailable" for every ticket.

    Returns:
        A callable matching ``PrFinder``: ticket id -> merged PRs whose
        ``headRefName`` names that exact ticket (see
        ``_branch_matches_ticket``) — not `gh`'s own text search and not
        plain substring containment, either of which lets an incidental
        mention (or a numeric-prefix collision between ticket ids) produce a
        false match.
    """

    def finder(fre_id: str) -> list[dict[str, object]] | None:
        if merged_prs is None:
            return None
        return [
            pr
            for pr in merged_prs
            if _branch_matches_ticket(str(pr.get("headRefName", "")), fre_id)
        ]

    return finder


@dataclasses.dataclass(frozen=True)
class ReconcileRun:
    """Result of one full reconciliation run.

    Attributes:
        claims: The fetched Linear claim set (ticket id -> state name). Empty
            here means the fetch itself found nothing to check at all (no
            key, or a query/network failure) — a distinct condition from
            ``verdicts`` being empty, which can happen on a healthy day when
            every fetched ticket legitimately has nothing to check yet.
        verdicts: One verdict per ticket with something to check.
    """

    claims: dict[str, str]
    verdicts: list[Verdict]


def reconcile(api_key: str | None = None, since_days: int = 90) -> ReconcileRun:
    """Run the full reconciliation and return the claim set plus all verdicts.

    The Linear claim fetch and the merged-PR fetch are independent I/O with
    no data dependency between them, so they run concurrently. The merged-PR
    fetch is skipped entirely when there is no key — it would be wasted work
    otherwise, since an empty claim set short-circuits to zero verdicts
    regardless.

    Args:
        api_key: Linear API key. Defaults to ``load_linear_key()``.
        since_days: Recency window for the terminal-state claim bucket.

    Returns:
        The fetched claim set plus one verdict per ticket with something to
        check (see ``verdict_for_claim``).
    """
    key = api_key if api_key is not None else load_linear_key()
    if not key:
        return ReconcileRun(claims={}, verdicts=[])
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        claims_future = pool.submit(fetch_board_claims, key, since_days)
        prs_future = pool.submit(_fetch_merged_prs)
        claims = claims_future.result()
        merged_prs = prs_future.result()
    if not claims:
        return ReconcileRun(claims={}, verdicts=[])
    pr_finder = _make_pr_finder(merged_prs)
    return ReconcileRun(claims=claims, verdicts=reconcile_claims_vs_repo(claims, pr_finder))


def _print_table(verdicts: Sequence[Verdict]) -> None:
    """Print verdicts as a human-readable report to stdout."""
    counts = {"PASS": 0, "FAIL": 0, "UNVERIFIABLE": 0}
    for verdict in verdicts:
        counts[verdict.status] += 1
        print(f"[{verdict.status:<12}] {verdict.claim}")
        print(f"               note: {verdict.note}")
        for cite in verdict.evidence:
            print(f"               evidence: {cite}")
    print("-" * 72)
    print(
        f"{len(verdicts)} verdicts — "
        f"{counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['UNVERIFIABLE']} UNVERIFIABLE"
    )


def exit_code_for(verdicts: Sequence[Verdict]) -> int:
    """Compute the process exit code from a set of verdicts.

    Purely "did anything FAIL" — an empty ``verdicts`` list is **not**
    treated as a forced failure here, because it is a legitimate, common
    outcome (a healthy day where every fetched ticket had nothing to check).
    The "nothing was checked at all" failure mode lives one level up, on the
    fetched *claim set* being empty (see ``main``) — conflating the two was
    a found bug: it turned every healthy day into a false-alarm exit 1.

    Args:
        verdicts: A verdict list, e.g. from a ``ReconcileRun``.

    Returns:
        ``1`` if any verdict is ``FAIL``, else ``0``.
    """
    return 1 if any(v.status == "FAIL" for v in verdicts) else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns ``1`` if any verdict is ``FAIL`` or no claims were fetched."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=90,
        help="Recency window in days for the Done/Awaiting-Deploy/Verify-Failed/Canceled/"
        "Duplicate claim bucket (default: 90). Every other state has no bound.",
    )
    parser.add_argument("--json", action="store_true", help="Emit verdicts as JSON.")
    args = parser.parse_args(argv)

    run = reconcile(since_days=args.since_days)

    if not run.claims:
        print(
            "ERROR: reconciler fetched zero ticket claims from Linear — this reads as a false "
            "pass. Check AGENT_LINEAR_API_KEY; refusing to exit clean.",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps([dataclasses.asdict(v) for v in run.verdicts], indent=2))
    else:
        _print_table(run.verdicts)

    return exit_code_for(run.verdicts)


if __name__ == "__main__":
    raise SystemExit(main())
