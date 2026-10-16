"""FRE-1610 AC-4 — replay the outcomes of the six canceled tickets.

Reads each ticket, classifies it with the production outcome rules, and reports whether
its fingerprint would be suppressed. The default is a dry run: nothing is written.

Why a replay is needed: outcome ingestion only visits tickets that sysgraph holds. The
FRE-1609 reset emptied sysgraph, so the six tickets that were canceled before the fix never
enter that queue. ``--apply`` writes their suppression once.

Run (dry run, reads Linear):
    uv run python -m scripts.fre1610_replay_cancellations

Run without a Linear key, from exported issues (a JSON list of ``get_issue`` dicts):
    uv run python -m scripts.fre1610_replay_cancellations --issues-json issues.json

Write the suppression (a production write; master runs it once after deploy):
    uv run python -m scripts.fre1610_replay_cancellations --apply

Exit status is 1 when any ticket would not be suppressed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from personal_agent.brainstem.jobs.outcome_ingestion import (
    _classify_outcome,
    fingerprint_to_suppress,
)
from personal_agent.captains_log.suppression import record_rejection_suppression

TICKETS: tuple[str, ...] = (
    "FRE-1373",
    "FRE-1378",
    "FRE-1404",
    "FRE-1412",
    "FRE-1418",
    "FRE-1429",
)


class _IssueReader(Protocol):
    async def get_issue(self, issue_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ReplayRow:
    """The replayed result for one ticket."""

    ticket: str
    state: str | None
    outcome: str | None
    fingerprint: str | None
    suppressed: bool


def load_issues_json(path: pathlib.Path) -> dict[str, dict[str, Any]]:
    """Load exported issues, keyed by identifier.

    Args:
        path: A JSON file holding a list of ``LinearClient.get_issue()``-shaped dicts,
            each with an ``identifier``.

    Returns:
        The issues keyed by ``identifier``.
    """
    issues = json.loads(path.read_text(encoding="utf-8"))
    return {str(issue["identifier"]): issue for issue in issues}


class _FileReader:
    def __init__(self, issues: dict[str, dict[str, Any]]) -> None:
        self._issues = issues

    async def get_issue(self, issue_id: str) -> dict[str, Any]:
        return self._issues.get(issue_id, {})


async def replay(client: _IssueReader, tickets: Sequence[str], *, apply: bool) -> list[ReplayRow]:
    """Replay the suppression decision for each ticket.

    Args:
        client: Reads a ticket by identifier.
        tickets: Ticket identifiers to replay.
        apply: ``True`` writes the suppression for each ticket that qualifies. ``False``
            changes nothing.

    Returns:
        One row per ticket, in the order given.
    """
    rows: list[ReplayRow] = []
    for ticket in tickets:
        issue = await client.get_issue(ticket)
        fingerprint = fingerprint_to_suppress(issue)
        if apply and fingerprint:
            record_rejection_suppression(fingerprint, issue_identifier=ticket)
        state = (issue.get("state") or {}).get("name")
        rows.append(
            ReplayRow(
                ticket=ticket,
                state=state,
                outcome=_classify_outcome(issue),
                fingerprint=fingerprint,
                suppressed=fingerprint is not None,
            )
        )
    return rows


async def _main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the suppression")
    parser.add_argument(
        "--issues-json", type=pathlib.Path, help="read exported issues instead of Linear"
    )
    args = parser.parse_args(argv)

    client: _IssueReader
    if args.issues_json:
        client = _FileReader(load_issues_json(args.issues_json))
    else:
        from personal_agent.captains_log.linear_client import LinearClient

        client = LinearClient()

    rows = await replay(client, TICKETS, apply=args.apply)
    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"FRE-1610 replay ({mode})")  # noqa: T201 - CLI report
    for row in rows:
        verdict = "SUPPRESSED" if row.suppressed else "NOT SUPPRESSED"
        print(  # noqa: T201 - CLI report
            f"{row.ticket}  state={row.state}  outcome={row.outcome}  "
            f"fingerprint={row.fingerprint}  {verdict}"
        )
    return 0 if all(row.suppressed for row in rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
