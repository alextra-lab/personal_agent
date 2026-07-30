"""CLI for the telemetry delivery-ratio probe — ADR-0090's fourth corner (FRE-1051).

Compares an event family in the log indices against an independent oracle (Postgres
``api_costs``) over a stated window, and exits non-zero when delivery falls below the
floor — so a future regression in delivery is visible rather than rediscovered by
accident.

Exit codes, which a standing monitor must be able to tell apart:

- ``0`` delivery passed.
- ``1`` a real verdict of breach, or nothing verifiable. The telemetry is at fault.
- ``2`` argparse rejected the arguments (argparse's own code, not ours).
- ``64`` a precondition failed — window inverted, or elasticsearch not installed.
- ``70`` the probe could not measure: Postgres or Elasticsearch was unreachable or
  errored. Deliberately distinct from ``1`` — "the probe is broken" and "delivery is
  broken" need different triage, and collapsing them onto one code loses that.

Usage::

    uv run python -m scripts.monitors.delivery_ratio_monitor
    uv run python -m scripts.monitors.delivery_ratio_monitor --since 2026-07-23 --until 2026-07-28
    uv run python -m scripts.monitors.delivery_ratio_monitor --json

``personal_agent`` imports are deliberately deferred into the functions below. Importing
the config package emits startup diagnostics on stdout, and under ``--json`` that would
corrupt the payload; keeping the imports late lets :func:`main` divert the descriptor
first.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from personal_agent.observability.delivery_ratio.probe import DeliveryReport

_EX_USAGE: int = 64
"""A precondition failed (inverted window, missing elasticsearch package)."""

_EX_PROBE_FAILED: int = 70
"""The probe could not measure — substrate unreachable. Not a delivery verdict."""


def _parse_day(raw: str) -> date:
    """Parse a ``YYYY-MM-DD`` CLI argument into a date.

    Args:
        raw: Date string.

    Returns:
        Parsed date.

    Raises:
        argparse.ArgumentTypeError: If the value is not an ISO calendar date.
    """
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc).date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {raw!r}") from exc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    from personal_agent.observability.delivery_ratio.probe import DEFAULT_MIN_RATIO

    parser = argparse.ArgumentParser(
        prog="delivery-ratio-monitor",
        description="Telemetry delivery-ratio gate against an independent oracle (FRE-1051).",
    )
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    parser.add_argument(
        "--since",
        type=_parse_day,
        default=yesterday,
        help="First UTC day of the window, inclusive (default: yesterday).",
    )
    parser.add_argument(
        "--until",
        type=_parse_day,
        default=yesterday,
        help="Last UTC day of the window, inclusive (default: yesterday).",
    )
    parser.add_argument(
        "--min-ratio",
        type=float,
        default=DEFAULT_MIN_RATIO,
        help=f"Delivery floor below which a family is a breach (default: {DEFAULT_MIN_RATIO}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of text.",
    )
    return parser.parse_args(argv)


async def _measure(args: argparse.Namespace) -> DeliveryReport | int:
    """Load settings, query both substrates, and return the report.

    Args:
        args: Parsed CLI arguments.

    Returns:
        The report, or an int exit code when a precondition fails.
    """
    from personal_agent.config.settings import get_settings
    from personal_agent.observability.delivery_ratio.collect import collect_report

    settings = get_settings()
    try:
        from elasticsearch import AsyncElasticsearch as ESClient
    except ModuleNotFoundError:
        sys.stderr.write("elasticsearch package is required\n")
        return _EX_USAGE

    import asyncpg  # type: ignore[import-untyped]

    from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn

    es = ESClient([settings.elasticsearch_url], request_timeout=60)
    conn = await asyncpg.connect(_normalize_asyncpg_dsn(settings.database_url))
    try:
        return await collect_report(
            es,
            conn,
            logs_prefix=settings.elasticsearch_index_prefix,
            since=args.since,
            until=args.until,
            min_ratio=args.min_ratio,
        )
    finally:
        await conn.close()
        await es.close()


@contextlib.contextmanager
def _stdout_to_stderr() -> Iterator[None]:
    """Divert file descriptor 1 to descriptor 2 for the duration of the block.

    ``contextlib.redirect_stdout`` is not sufficient: structlog's handler captures its
    stream when logging is configured and keeps writing to the original object.
    Redirecting the underlying descriptor catches every writer, including ones that
    ran at import time.

    Yields:
        None, with descriptor 1 pointing at descriptor 2.
    """
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


def _emit(outcome: Any, *, as_json: bool) -> int:
    """Write the report to stdout and return the process exit code.

    Args:
        outcome: A report, or an int exit code from a failed precondition.
        as_json: Whether to emit JSON rather than text.

    Returns:
        Process exit code.
    """
    from personal_agent.observability.delivery_ratio.probe import render_report

    if isinstance(outcome, int):
        return outcome
    if as_json:
        sys.stdout.write(json.dumps(outcome.to_dict(), indent=2) + "\n")
    else:
        sys.stdout.write(render_report(outcome) + "\n")
    return int(outcome.exit_code)


def _warm_config_imports() -> None:
    """Import the config package so its startup logging happens under a redirect.

    Importing ``personal_agent.config`` emits startup diagnostics on stdout exactly
    once. Forcing that here, inside the caller's redirect, lets argparse and ``--help``
    run afterwards against a real stdout with nothing left to pollute it.
    """
    import personal_agent.config.settings  # noqa: F401
    import personal_agent.observability.delivery_ratio.probe  # noqa: F401


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector, defaulting to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    raw = sys.argv[1:] if argv is None else argv
    as_json = "--json" in raw

    # Argument parsing stays OUTSIDE the redirect so `--help` reaches the real stdout.
    # Parsing inside it sent the help text to stderr and exited 0 with empty stdout —
    # which a wrapper reading stdout and gating on the exit code would score as a
    # silent green, the failure mode this probe exists to eliminate.
    if as_json:
        with _stdout_to_stderr():
            _warm_config_imports()

    args = _parse_args(raw)
    if args.until < args.since:
        sys.stderr.write("--until must not precede --since\n")
        return _EX_USAGE

    try:
        if as_json:
            with _stdout_to_stderr():
                outcome = asyncio.run(_measure(args))
        else:
            outcome = asyncio.run(_measure(args))
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed; see _EX_PROBE_FAILED
        sys.stderr.write(f"delivery-ratio probe failed to measure: {exc!r}\n")
        return _EX_PROBE_FAILED

    return _emit(outcome, as_json=as_json)


if __name__ == "__main__":
    sys.exit(main())
