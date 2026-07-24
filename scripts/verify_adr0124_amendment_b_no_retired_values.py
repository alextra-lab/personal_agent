#!/usr/bin/env python3
r"""Amendment B verification: no retired value survives (ADR-0124, FRE-956).

Amendment B retired the `tool_evidence` basis and the `status_contradiction`
correction tier from the session-digest schema. This is the regression check the
amendment's own acceptance criterion names: over the population of digests
generated **after** the amendment deployed, zero items may carry either retired
value — a build that edited the prose but left the schema enum or the producer
prompt intact would produce one.

This is **not** the verification oracle (a separate, later fact-checking system —
Lane 5 / Workstream 4). It is a narrow, read-only regression scan, scoped entirely
to this one acceptance criterion.

**The population must be non-empty for a real post-deploy check.** An empty
population would let the scan pass vacuously while the producer might be dead, so
this fails loudly by default; `--allow-empty` exists only for this script's own
tests and dry-runs, never for the real post-deploy verification the ADR describes.

    uv run python scripts/verify_adr0124_amendment_b_no_retired_values.py \\
        --deploy-timestamp 2026-07-24T12:00:00+00:00

This writes nothing to any substrate — a single read-only Cypher query.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import orjson

_QUERY = """\
MATCH (s:Session)
WHERE s.summary_generated_at IS NOT NULL
  AND s.summary_generated_at > $deploy_ts
  AND s.session_digest IS NOT NULL
RETURN s.session_digest AS digest
"""


class _DigestRecord(Protocol):
    """One returned row — only ``record["digest"]`` is ever read."""

    def __getitem__(self, key: str) -> str: ...


class _DigestResult(Protocol):
    """What a session's ``run()`` returns — an async iterable of records."""

    def __aiter__(self) -> AsyncIterator[_DigestRecord]: ...


class Neo4jSessionLike(Protocol):
    """The slice of the neo4j ``AsyncSession`` contract this module needs."""

    async def run(self, query: str, **params: Any) -> _DigestResult:
        """Run a Cypher query and return its result."""
        ...

    async def __aenter__(self) -> "Neo4jSessionLike":
        """Enter the session's async context."""
        ...

    async def __aexit__(self, *exc_info: object) -> None:
        """Exit the session's async context."""
        ...


class Neo4jDriverLike(Protocol):
    """The slice of the neo4j ``AsyncDriver`` contract this module needs.

    A real ``AsyncDriver`` satisfies this structurally; tests inject a small
    fake that does the same, with no live Neo4j required.
    """

    def session(self) -> Neo4jSessionLike:
        """Open a new session."""
        ...


@dataclass(frozen=True)
class RetiredValueScan:
    """Counts of retired schema values found across a population of digests.

    Attributes:
        population: Number of digests scanned.
        tool_evidence_count: Items (any slot) carrying the retired `tool_evidence`
            basis.
        status_contradiction_count: Corrections carrying the retired
            `status_contradiction` tier.
    """

    population: int
    tool_evidence_count: int
    status_contradiction_count: int

    @property
    def clean(self) -> bool:
        """Whether zero retired values were found."""
        return self.tool_evidence_count == 0 and self.status_contradiction_count == 0


def scan_digests(raw_digest_json: Sequence[str]) -> RetiredValueScan:
    """Scan a population of stored digest JSON strings for retired schema values.

    Pure and DB-free: takes the already-fetched ``session_digest`` property
    values and counts occurrences of the two values Amendment B retired.

    Args:
        raw_digest_json: Each session's stored ``session_digest`` JSON string.

    Returns:
        The scan result.
    """
    tool_evidence = 0
    status_contradiction = 0
    for raw in raw_digest_json:
        parsed: dict[str, Any] = orjson.loads(raw)
        for slot in ("established", "decisions", "unresolved", "corrections"):
            for item in parsed.get(slot) or []:
                if item.get("basis") == "tool_evidence":
                    tool_evidence += 1
        for correction in parsed.get("corrections") or []:
            if correction.get("tier") == "status_contradiction":
                status_contradiction += 1

    return RetiredValueScan(
        population=len(raw_digest_json),
        tool_evidence_count=tool_evidence,
        status_contradiction_count=status_contradiction,
    )


async def run_scan(
    driver: Neo4jDriverLike, deploy_ts: datetime, *, allow_empty: bool = False
) -> RetiredValueScan:
    """Fetch the post-deploy digest population and scan it.

    Args:
        driver: Anything exposing the neo4j ``AsyncDriver``-shaped ``.session()``
            async context manager — a real driver in production, an injectable
            fake in tests.
        deploy_ts: Digests generated at or before this timestamp are excluded —
            the population is strictly the post-Amendment-B set.
        allow_empty: Unused here; the empty-population policy lives in
            :func:`verdict`, not the fetch. Accepted for call-site symmetry with
            the CLI's flag of the same name.

    Returns:
        The scan result.
    """
    del allow_empty  # policy lives in verdict(); fetching never depends on it
    async with driver.session() as session:
        result = await session.run(_QUERY, deploy_ts=deploy_ts.isoformat())
        raw_digests = [record["digest"] async for record in result]

    return scan_digests(raw_digests)


def verdict(scan: RetiredValueScan, *, allow_empty: bool = False) -> str | None:
    """Apply the pass/fail policy to a scan result.

    Args:
        scan: The scan to judge.
        allow_empty: Whether an empty population is acceptable. Must stay
            ``False`` for a real post-deploy check — see the module docstring.

    Returns:
        ``None`` if the scan passes; otherwise a human-readable failure reason.
    """
    if scan.population == 0 and not allow_empty:
        return (
            "population is empty — this would let the scan pass vacuously while "
            "the producer could be dead; use --allow-empty only for tests/dry-runs"
        )
    if scan.tool_evidence_count:
        return f"{scan.tool_evidence_count} item(s) carry the retired tool_evidence basis"
    if scan.status_contradiction_count:
        return (
            f"{scan.status_contradiction_count} correction(s) carry the retired "
            "status_contradiction tier"
        )
    return None


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deploy-timestamp",
        required=True,
        type=datetime.fromisoformat,
        help="ISO-8601 timestamp of the Amendment B deploy; only digests generated after "
        "this are scanned",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        default=False,
        help="Allow an empty population to pass (tests/dry-runs only — never for a real "
        "post-deploy check)",
    )
    return parser


async def _main() -> int:
    args = build_arg_parser().parse_args()

    from personal_agent.memory.service import MemoryService  # noqa: PLC0415

    service = MemoryService()
    if not await service.connect() or service.driver is None:
        print("could not connect to Neo4j", file=sys.stderr)
        return 1

    try:
        scan = await run_scan(service.driver, args.deploy_timestamp, allow_empty=args.allow_empty)
    finally:
        await service.disconnect()

    reason = verdict(scan, allow_empty=args.allow_empty)
    report = {
        "deploy_timestamp": args.deploy_timestamp.isoformat(),
        "population": scan.population,
        "tool_evidence_count": scan.tool_evidence_count,
        "status_contradiction_count": scan.status_contradiction_count,
        "passed": reason is None,
        "failure_reason": reason,
    }
    print(orjson.dumps(report, option=orjson.OPT_INDENT_2).decode())
    return 0 if reason is None else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
