"""Robust structlog record capture for tests exercising already-imported production loggers.

ADR-0129 D3/D4 (FRE-1069). ``structlog.testing.capture_logs()`` and pytest's ``caplog``
fixture both silently miss records from a module-level ``log = get_logger(__name__)``
logger that some *other* test or import already triggered earlier in the same pytest
session: ``structlog.configure(..., cache_logger_on_first_use=True)`` freezes each
``BoundLoggerLazyProxy`` INSTANCE's processor chain the first time *that specific
instance* logs, and neither ``capture_logs()`` (which swaps the *global* processor list
after the freeze already happened) nor ``caplog`` reliably observes it. A plain
``logging.Handler`` attached directly to the root stdlib logger does not have this
problem: every structlog record — cached logger or not — still funnels through a real
``logging.Logger.handle()`` call via ``structlog.stdlib.LoggerFactory()``, which this
handler intercepts regardless of which processor-chain snapshot produced it.

Use this whenever the code under test calls into a *production* module's own
pre-existing logger (scheduler.py, consolidator.py, the observability scheduler
runners, service/app.py) rather than a logger constructed fresh inside the test itself
— for a fresh, never-before-used logger name, ``structlog.testing.capture_logs()``
remains simpler and is not affected by this issue.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from typing import Any

import structlog

from personal_agent.telemetry.logger import configure_logging


class _ListHandler(logging.Handler):
    def __init__(self, records: list[MutableMapping[str, Any]]) -> None:
        super().__init__()
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        # ProcessorFormatter.wrap_for_formatter stashes the fully-processed
        # structlog event_dict directly as record.msg — no string parsing needed.
        if isinstance(record.msg, dict):
            self._records.append(record.msg)


@contextmanager
def capture_log_records() -> Iterator[list[MutableMapping[str, Any]]]:
    """Capture every structlog event dict emitted while the context is active.

    Calls ``configure_logging()`` only if structlog has never been configured in
    this process — matching the same lazy guard ``telemetry.get_logger()`` itself
    uses. Unconditionally re-calling it (the original design here) is actively
    harmful in a full test-suite run: `structlog.configure(..., cache_logger_on
    _first_use=True)` freezes each logger's processor chain the first time THAT
    specific bound-logger instance emits, so re-running `configure_logging()`
    mid-suite causes whatever OTHER production logger happens to fire next
    (in this test or a later, unrelated one) to freeze against a *fresh* chain
    outside of that other test's own `capture_logs()`/`caplog` window — silently
    breaking it. Once structlog is configured at all (which happens once, very
    early, via the normal import chain in any real test run), the real
    processor chain — including ``_add_span_context`` — is already active and
    reconfiguring buys nothing.

    Yields:
        A list that fills with each record's event dict, in emission order.
    """
    if not structlog.is_configured():
        configure_logging()
    records: list[MutableMapping[str, Any]] = []
    handler = _ListHandler(records)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield records
    finally:
        root.removeHandler(handler)
