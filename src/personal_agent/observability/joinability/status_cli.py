"""CLI for the 7-day green gate (``make joinability-status``).

Prints an ASCII table summarising the last seven days of probe runs and
exits 0 only when the green-gate predicate is satisfied. Intended for
manual review prior to flipping ADR-0074 Proposed → Accepted.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from personal_agent.config.settings import get_settings
from personal_agent.observability.joinability.status import (
    DEFAULT_MIN_RUNS_PER_DAY,
    DEFAULT_WINDOW_DAYS,
    compute_seven_day_gate,
    render_table,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="joinability-status",
        description="7-day green-gate verdict for the joinability probe.",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help="Lookback window in days (default: 7).",
    )
    parser.add_argument(
        "--min-runs-per-day",
        type=int,
        default=DEFAULT_MIN_RUNS_PER_DAY,
        help="Minimum non-skipped runs required per day (default: 12).",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    try:
        from elasticsearch import AsyncElasticsearch as ESClient
    except ModuleNotFoundError:
        sys.stderr.write("elasticsearch package is required for joinability-status\n")
        return 64

    es = ESClient([settings.elasticsearch_url], request_timeout=30)
    try:
        gate = await compute_seven_day_gate(
            es,
            prefix=settings.joinability_probe_index_prefix,
            window_days=args.window_days,
            min_runs_per_day=args.min_runs_per_day,
        )
    finally:
        await es.close()
    sys.stdout.write(render_table(gate) + "\n")
    return 0 if gate.status == "green" else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``make joinability-status``."""
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
