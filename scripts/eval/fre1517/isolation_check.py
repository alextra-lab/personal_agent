"""FRE-1517 AC-3 — the cross-session isolation check, run after each arm.

Each session's first turn wipes ``neo4j-eval`` (A1). So after an arm, the eval graph must hold
nodes from the arm's latest session only. This check reads every node's
``originating_session_id`` (``fetch_originating_session_ids``) and every session id the run's
JSONL rows recorded. It then applies ``find_cross_session_sources`` once per earlier session.

The output is admissible for a zero, per the explore skill's rule:

- ``nodes_by_session`` shows the latest session's own nodes. That is a raw instance of the
  identifier in the queried store (arm 1a), and it shows the query reaches the graph (arm 2).
- ``scope`` names the run ids, the sessions and the store (arm 3).

A leak does not fail AC-3, but it marks every later arm's results contaminated.

Run from the repo root, after an arm's last session has settled:

    export NEO4J_PASSWORD=...
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. uv run python scripts/eval/fre1517/isolation_check.py --run-id <id> [--run-id <id> ...]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import UTC, datetime

from scripts.eval.eval_isolation import create_eval_driver
from scripts.eval.fre1337_intent_probe.substrate import (
    EVAL_NEO4J_URI,
    fetch_originating_session_ids,
    find_cross_session_sources,
)
from scripts.eval.fre1517.session_runner import HERE


def sessions_in_order(run_ids: list[str]) -> list[tuple[str, str]]:
    """Every session id the runs recorded, ordered by the session's first turn.

    Args:
        run_ids: Run ids whose JSONL rows live under ``out/<run_id>/``.

    Returns:
        ``(started_at, session_id)`` pairs, oldest first, one per session.
    """
    first_seen: dict[str, str] = {}
    for run_id in run_ids:
        for path in sorted((HERE / "out" / run_id).glob("*.jsonl")):
            if path.name.endswith(".ttft.jsonl"):
                continue
            for line in path.read_text().splitlines():
                row = json.loads(line) if line.strip() else {}
                sid, started = row.get("session_id"), row.get("started_at")
                if sid and started and (sid not in first_seen or started < first_seen[sid]):
                    first_seen[sid] = started
    return sorted((started, sid) for sid, started in first_seen.items())


async def run(run_ids: list[str]) -> int:
    """Run the check and print its evidence as JSON.

    Args:
        run_ids: The run ids to read sessions from.

    Returns:
        0 when no earlier session's node remains, 1 on a leak, 2 when no session was found.
    """
    ordered = sessions_in_order(run_ids)
    if not ordered:
        sys.stderr.write(f"no sessions recorded under {run_ids}\n")
        return 2
    latest = ordered[-1][1]
    driver = create_eval_driver()
    try:
        nodes = await fetch_originating_session_ids(driver, uri=EVAL_NEO4J_URI)
    finally:
        await driver.close()
    # Any session other than the latest is a leak, including sessions from runs not named here.
    # A check against recorded sessions only missed three older sessions' 45 nodes on 2026-09-14.
    foreign = {n.get("originating_session_id") for n in nodes} - {latest}
    leaked = {sid: find_cross_session_sources(nodes, sid) for sid in sorted(foreign, key=str)}
    report = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "scope": {"store": EVAL_NEO4J_URI, "run_ids": run_ids, "sessions_oldest_first": ordered},
        "latest_session": latest,
        "nodes_total": len(nodes),
        "nodes_by_session": dict(Counter(n.get("originating_session_id") for n in nodes)),
        "leaked_by_earlier_session": {sid: len(r) for sid, r in leaked.items()},
        "leaked_examples": {sid: r[:5] for sid, r in leaked.items()},
    }
    sys.stdout.write(json.dumps(report, indent=2, default=str) + "\n")
    return 1 if leaked else 0


def main() -> int:
    """Parse arguments and run the check.

    Returns:
        The exit code from :func:`run`.
    """
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", action="append", required=True)
    return asyncio.run(run(p.parse_args().run_id))


if __name__ == "__main__":
    sys.exit(main())
