r"""FRE-1417 — reproduce the unmeasured-claim detector's alert count on live data.

``detect_unmeasured_claim`` (``personal_agent.orchestrator.unmeasured_claim``)
flags a sub-agent that reports a computational performance result while its
own capture record shows zero tool executions. AC-5 requires the detector's
false-positive rate to be measured, not assumed. This probe reproduces that
measurement against live Elasticsearch without ever printing ``full_output``
— the corpus is real production usage and may contain personal content, so
this script reports only identifiers, a digest, and the verdict. Human
adjudication of whether an alert is a genuine positive is not automated; that
review is recorded in ``tests/personal_agent/orchestrator/test_unmeasured_claim.py``'s
module docstring for the sample taken on 2026-09-06.

Usage::

    python -m scripts.research.fre1417_unmeasured_claim_probe sample --days 7
    python -m scripts.research.fre1417_unmeasured_claim_probe replay \\
        --trace-id <trace_id> --task-id <task_id>

This module only reads existing telemetry (Elasticsearch) — it never writes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch

log = structlog.get_logger(__name__)


async def _fetch_zero_tool_captures(
    es_client: "AsyncElasticsearch", since: datetime, min_output_chars: int
) -> list[dict[str, object]]:
    """Every zero-tool-call sub-agent capture since ``since`` with non-trivial output.

    Args:
        es_client: An open ``AsyncElasticsearch`` client.
        since: Only captures at or after this timestamp.
        min_output_chars: Skip captures whose ``full_output`` is shorter than this
            (empty/near-empty output can never match the detector).

    Returns:
        Raw ES ``_source`` documents, oldest first; empty on any query failure.
    """
    from personal_agent.captains_log.capture import SUBAGENT_CAPTURES_INDEX_PREFIX

    try:
        response = await es_client.search(
            index=f"{SUBAGENT_CAPTURES_INDEX_PREFIX}-*",
            query={"range": {"timestamp": {"gte": since.isoformat()}}},
            sort=[{"timestamp": "asc"}],
            size=1000,
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception as exc:
        log.warning("unmeasured_claim_probe_query_failed", error=str(exc))
        return []

    docs = []
    for hit in response.get("hits", {}).get("hits", []) or []:
        source = hit.get("_source")
        if not isinstance(source, dict):
            continue
        if source.get("tools_used"):
            continue
        if len(source.get("full_output", "") or "") < min_output_chars:
            continue
        docs.append(source)
    return docs


async def _fetch_one_capture(
    es_client: "AsyncElasticsearch", trace_id: str, task_id: str
) -> dict[str, object] | None:
    from personal_agent.captains_log.capture import SUBAGENT_CAPTURES_INDEX_PREFIX

    response = await es_client.search(
        index=f"{SUBAGENT_CAPTURES_INDEX_PREFIX}-*",
        query={
            "bool": {
                "must": [
                    {"term": {"trace_id": trace_id}},
                    {"term": {"task_id": task_id}},
                ]
            }
        },
        size=1,
    )
    hits = response.get("hits", {}).get("hits", []) or []
    if not hits:
        return None
    source = hits[0].get("_source")
    return source if isinstance(source, dict) else None


def _run_sample(days: int, min_output_chars: int) -> int:
    from personal_agent.orchestrator.unmeasured_claim import detect_unmeasured_claim

    async def _run() -> int:
        from elasticsearch import AsyncElasticsearch

        from personal_agent.config import settings

        es_client = AsyncElasticsearch(hosts=[settings.elasticsearch_url])
        try:
            since = datetime.now(timezone.utc) - timedelta(days=days)
            docs = await _fetch_zero_tool_captures(es_client, since, min_output_chars)
            alerts = [
                doc for doc in docs if detect_unmeasured_claim([], str(doc.get("full_output", "")))
            ]
            print(f"scanned:  {len(docs)} zero-tool-call captures since {since.isoformat()}")
            print(f"alerted:  {len(alerts)}")
            for doc in alerts:
                # spec_task, like full_output, is derived from the user's real request —
                # never printed verbatim; only identifiers and a digest cross this boundary.
                digest = hashlib.sha256(str(doc.get("full_output", "")).encode()).hexdigest()
                print(
                    f"  trace_id={doc.get('trace_id')} task_id={doc.get('task_id')} sha256={digest}"
                )
            return 0
        finally:
            await es_client.close()

    return asyncio.run(_run())


def _run_replay(trace_id: str, task_id: str) -> int:
    from personal_agent.orchestrator.unmeasured_claim import detect_unmeasured_claim

    async def _run() -> int:
        from elasticsearch import AsyncElasticsearch

        from personal_agent.config import settings

        es_client = AsyncElasticsearch(hosts=[settings.elasticsearch_url])
        try:
            doc = await _fetch_one_capture(es_client, trace_id, task_id)
            if doc is None:
                print(f"no capture found for trace_id={trace_id} task_id={task_id}")
                return 2
            full_output = str(doc.get("full_output", ""))
            raw_tools_used = doc.get("tools_used") or []
            tools_used = (
                [str(t) for t in raw_tools_used] if isinstance(raw_tools_used, list) else []
            )
            verdict = detect_unmeasured_claim(tools_used, full_output)
            digest = hashlib.sha256(full_output.encode()).hexdigest()
            print(f"trace_id:   {trace_id}")
            print(f"task_id:    {task_id}")
            print(f"tools_used: {tools_used}")
            print(f"sha256:     {digest}")
            print(f"flagged:    {verdict}")
            return 0
        finally:
            await es_client.close()

    return asyncio.run(_run())


def main() -> None:
    """CLI entry point: dispatches to `sample` or `replay`."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample_parser = subparsers.add_parser(
        "sample", help="Run the detector over recent zero-tool-call captures and count alerts."
    )
    sample_parser.add_argument("--days", type=int, default=7)
    sample_parser.add_argument("--min-output-chars", type=int, default=30)

    replay_parser = subparsers.add_parser(
        "replay", help="Run the detector against one named capture and print its verdict."
    )
    replay_parser.add_argument("--trace-id", required=True)
    replay_parser.add_argument("--task-id", required=True)

    args = parser.parse_args()
    if args.command == "sample":
        sys.exit(_run_sample(args.days, args.min_output_chars))
    else:
        sys.exit(_run_replay(args.trace_id, args.task_id))


if __name__ == "__main__":
    main()
