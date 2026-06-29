#!/usr/bin/env python3
"""Deterministic, read-only delivery-board reconciler (FRE-680).

Maps board *claims* to durable *evidence* and emits verdicts. No LLM. The three
evidence sources are:

* **Linear** ticket state (GraphQL API, stdlib ``urllib``),
* **merged PRs** whose branch maps to a ticket (``gh`` CLI),
* the **MASTER_PLAN.md** header narrative.

Each verdict has exactly four fields: ``claim``, ``status`` (one of ``PASS`` /
``FAIL`` / ``UNVERIFIABLE``), ``evidence`` (a list of citations), and ``note``.
``UNVERIFIABLE`` (no source to check) is a first-class outcome and is never
silently treated as ``PASS``.

Callable by hand, from prime-master, and from the master post-merge step::

    python scripts/reconcile_board.py            # human-readable table
    python scripts/reconcile_board.py --json      # machine-readable verdict list

Exit code is ``1`` if any verdict is ``FAIL`` (drift detected), else ``0``.

See ``.claude/skills/lifecycle-rules.md`` § Evidence contract (proof of Done).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

Status = Literal["PASS", "FAIL", "UNVERIFIABLE"]

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
LINEAR_TEAM_KEY = "FRE"

# Linear state names that mean the ticket is closed (terminal).
_CLOSED_STATE_NAMES: frozenset[str] = frozenset({"done", "canceled", "cancelled", "duplicate"})

# Present-tense assertions that a ticket is still open. These are authoritative
# current-state claims (master wrote them knowingly) and win over bare history.
_OPEN_PHRASES: tuple[str, ...] = (
    r"stays In Progress",
    r"kept In Progress",
    r"held In Progress",
    r"stays OPEN",
    r"kept OPEN",
    r"closes only when",
)
# Words indicating a ticket was completed.
_DONE_PHRASES: tuple[str, ...] = (
    r"\bDONE\b",
    r"\bDone\b",
    r"\bSHIPPED\b",
    r"\bDEPLOYED\b",
    r"closed Done",
    r"\bmerged\b",
)

_FRE_RE = re.compile(r"FRE-\d+")
# Split the header into clauses; parens isolate parentheticals like
# "(FRE-655 stays In Progress)" into their own clause.
_CLAUSE_SPLIT_RE = re.compile(r"[.;·()\n]|—")


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


def extract_header(master_plan_text: str) -> str:
    """Return the ``> **Last updated**:`` header paragraph of MASTER_PLAN.

    The header is where master narrates current state; the body below is the
    live plan with checkboxes. Acceptance criterion AC1 scopes the drift check
    to "the header narrates".

    Args:
        master_plan_text: Full text of ``docs/plans/MASTER_PLAN.md``.

    Returns:
        The header block as a single string, or an empty string if not found.
    """
    lines = master_plan_text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if "**Last updated**" in line),
        None,
    )
    if start is None:
        return ""
    collected: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped or not stripped.startswith(">"):
            break
        collected.append(stripped.lstrip("> ").rstrip())
    return " ".join(collected)


def _clause_binds_open(clause: str) -> bool:
    """Return True if the clause asserts a *current* open state.

    Explicit present-tense phrases (``stays In Progress``) always count. A bare
    ``In Progress`` counts only when not preceded by past-tense ``was``/``were``
    (so "was In Progress" is treated as history, not a current claim).
    """
    if any(re.search(p, clause, re.IGNORECASE) for p in _OPEN_PHRASES):
        return True
    if re.search(r"\b(?:was|were)\s+in progress\b", clause, re.IGNORECASE):
        return False
    return bool(re.search(r"\bin progress\b", clause, re.IGNORECASE))


def _clause_binds_done(clause: str) -> bool:
    """Return True if the clause asserts the ticket completed/shipped."""
    return any(re.search(p, clause) for p in _DONE_PHRASES)


def classify_header_claims(header_text: str) -> dict[str, str]:
    """Classify each ticket the header references as ``OPEN`` or ``DONE``.

    Splits the header into clauses and binds state per ticket from phrases in
    the same clause. ``OPEN`` is authoritative: a single current-open clause
    fixes the ticket as ``OPEN`` regardless of historical ``DONE`` words
    elsewhere. Tickets with no state binding are omitted (treated as OTHER).

    Args:
        header_text: The MASTER_PLAN header paragraph.

    Returns:
        Mapping of ticket id (e.g. ``FRE-655``) to ``OPEN`` or ``DONE``.
    """
    claims: dict[str, str] = {}
    for clause in _CLAUSE_SPLIT_RE.split(header_text):
        tickets = set(_FRE_RE.findall(clause))
        if not tickets:
            continue
        is_open = _clause_binds_open(clause)
        is_done = _clause_binds_done(clause)
        for fre in tickets:
            if is_open:
                claims[fre] = "OPEN"
            elif is_done and claims.get(fre) != "OPEN":
                claims[fre] = "DONE"
    return claims


def reconcile_master_plan(
    claims: Mapping[str, str],
    linear_states: Mapping[str, str | None],
) -> list[Verdict]:
    """Reconcile MASTER_PLAN header claims against Linear ticket state.

    Args:
        claims: Ticket id -> ``OPEN`` / ``DONE`` from the header.
        linear_states: Ticket id -> Linear state name, or ``None`` when the
            state could not be fetched.

    Returns:
        One verdict per ticket. A claimed-open ticket that Linear shows closed
        (or vice versa) is ``FAIL``; an unfetchable state is ``UNVERIFIABLE``.
    """
    verdicts: list[Verdict] = []
    for fre in sorted(claims):
        claim = claims[fre]
        state = linear_states.get(fre)
        if state is None:
            verdicts.append(
                Verdict(
                    claim=f"MASTER_PLAN header narrates {fre} as {claim}",
                    status="UNVERIFIABLE",
                    evidence=[f"MASTER_PLAN.md header references {fre}"],
                    note="Linear state unavailable (no API key or request failed)",
                )
            )
            continue
        closed = state.strip().lower() in _CLOSED_STATE_NAMES
        if claim == "OPEN" and closed:
            status: Status = "FAIL"
            note = f"header narrates {fre} as open, but Linear shows {state}"
        elif claim == "DONE" and not closed:
            status = "FAIL"
            note = f"header narrates {fre} as done, but Linear shows {state}"
        else:
            status = "PASS"
            note = f"header claim ({claim}) agrees with Linear ({state})"
        verdicts.append(
            Verdict(
                claim=f"MASTER_PLAN header narrates {fre} as {claim}; Linear shows {state}",
                status=status,
                evidence=["MASTER_PLAN.md header", f"Linear {fre} status={state}"],
                note=note,
            )
        )
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


def fetch_linear_states(
    fre_ids: Sequence[str],
    api_key: str | None,
) -> dict[str, str | None]:
    """Fetch current Linear state names for the given ticket ids.

    Args:
        fre_ids: Ticket ids such as ``FRE-655``.
        api_key: Linear personal API key, or ``None``.

    Returns:
        Mapping of ticket id to its Linear state name. A ticket whose state
        could not be determined (no key, request error, not found) maps to
        ``None`` — never silently to a state.
    """
    states: dict[str, str | None] = {fre: None for fre in fre_ids}
    if not api_key or not fre_ids:
        return states
    numbers = [int(fre.split("-", 1)[1]) for fre in fre_ids]
    query = (
        "query Issues($numbers: [Float!]) {"
        f'  issues(filter: {{ team: {{ key: {{ eq: "{LINEAR_TEAM_KEY}" }} }},'
        "                    number: { in: $numbers } }) {"
        "    nodes { identifier state { name } }"
        "  }"
        "}"
    )
    payload = json.dumps({"query": query, "variables": {"numbers": numbers}}).encode()
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
        return states
    nodes = (data.get("data") or {}).get("issues", {}).get("nodes", [])
    for node in nodes:
        identifier = node.get("identifier")
        state = (node.get("state") or {}).get("name")
        if identifier in states:
            states[identifier] = state
    return states


def check_merged_pr(fre_id: str) -> Verdict:
    """Check whether a merged PR's branch maps to a Done ticket (Check B).

    Informational: a found PR is ``PASS``; an unreachable ``gh`` or no matching
    PR is ``UNVERIFIABLE`` (a missing PR may be a decision-only ticket — it is
    never silently failed).
    """
    try:
        out = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--search",
                fre_id,
                "--json",
                "number,headRefName,mergedAt",
                "--limit",
                "20",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return Verdict(
            claim=f"{fre_id} (Done) is backed by a merged PR",
            status="UNVERIFIABLE",
            evidence=[],
            note="gh CLI unavailable",
        )
    if out.returncode != 0:
        return Verdict(
            claim=f"{fre_id} (Done) is backed by a merged PR",
            status="UNVERIFIABLE",
            evidence=[],
            note=f"gh error: {out.stderr.strip()[:120]}",
        )
    try:
        prs = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return Verdict(
            claim=f"{fre_id} (Done) is backed by a merged PR",
            status="UNVERIFIABLE",
            evidence=[],
            note="gh returned unparseable JSON",
        )
    token = fre_id.lower()
    matches = [pr for pr in prs if token in str(pr.get("headRefName", "")).lower()]
    if matches:
        return Verdict(
            claim=f"{fre_id} (Done) is backed by a merged PR",
            status="PASS",
            evidence=[
                f"PR #{pr['number']} headRef={pr['headRefName']} mergedAt={pr['mergedAt']}"
                for pr in matches
            ],
            note="merged PR with branch mapping to the ticket found",
        )
    return Verdict(
        claim=f"{fre_id} (Done) is backed by a merged PR",
        status="UNVERIFIABLE",
        evidence=[],
        note="no merged PR with a matching branch — may be decision-only or branch naming",
    )


def _default_master_plan_path() -> Path:
    """Return the repo's ``docs/plans/MASTER_PLAN.md`` path (no hardcoded root)."""
    return Path(__file__).resolve().parent.parent / "docs" / "plans" / "MASTER_PLAN.md"


def reconcile(master_plan_path: Path) -> list[Verdict]:
    """Run the full reconciliation and return all verdicts.

    Args:
        master_plan_path: Path to ``MASTER_PLAN.md``.

    Returns:
        Check-A (MASTER_PLAN ↔ Linear) verdicts followed by Check-B
        (Done ↔ merged-PR) verdicts.
    """
    header = extract_header(master_plan_path.read_text())
    claims = classify_header_claims(header)
    fre_ids = sorted(claims)
    linear_states = fetch_linear_states(fre_ids, load_linear_key())
    verdicts = reconcile_master_plan(claims, linear_states)
    for fre in fre_ids:
        state = linear_states.get(fre)
        if claims.get(fre) == "DONE" or (state is not None and state.strip().lower() == "done"):
            verdicts.append(check_merged_pr(fre))
    return verdicts


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


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns ``1`` if any verdict is ``FAIL``, else ``0``."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--master-plan",
        type=Path,
        default=_default_master_plan_path(),
        help="Path to MASTER_PLAN.md (default: docs/plans/MASTER_PLAN.md).",
    )
    parser.add_argument("--json", action="store_true", help="Emit verdicts as JSON.")
    args = parser.parse_args(argv)

    verdicts = reconcile(args.master_plan)

    if args.json:
        print(json.dumps([dataclasses.asdict(v) for v in verdicts], indent=2))
    else:
        _print_table(verdicts)

    return 1 if any(v.status == "FAIL" for v in verdicts) else 0


if __name__ == "__main__":
    raise SystemExit(main())
