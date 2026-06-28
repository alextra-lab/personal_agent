"""FRE-491 — per-case substrate isolation for the memory-recall harness.

Codex plan-review (FRE-491) flagged a baseline-invalidating confound: the harness
loops every probe case against one shared test substrate with no wipe, and the
bespoke gate reuses entity names across cases under first-write-wins. So an
earlier case can seed/satisfy a later case's query (false pass) or freeze a
later case's description (false fail), and the true-negative abstention controls
get polluted by every prior case's entities.

The fix is ``wipe_substrate`` + the ``--wipe-between-cases`` flag: each case starts
from an empty graph. These tests pin the wipe Cypher, the TEST-substrate guard,
and the run-report provenance stamp.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts.eval.fre435_memory_recall import harness
from scripts.eval.fre435_memory_recall.harness import WIPE_CYPHER, wipe_substrate
from scripts.eval.fre435_memory_recall.report import RunReport, render_json, render_markdown

from personal_agent.config.env_loader import Environment


class _FakeSession:
    """Minimal async-context-manager stand-in for a Neo4j session."""

    def __init__(self) -> None:
        self.run = AsyncMock()

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _service_with_session(session: _FakeSession) -> MagicMock:
    """A MemoryService double whose ``driver.session()`` yields ``session``."""
    driver = MagicMock()
    driver.session = MagicMock(return_value=session)
    service = MagicMock()
    service.driver = driver
    return service


@pytest.mark.asyncio
async def test_wipe_runs_detach_delete_on_test_substrate(monkeypatch: pytest.MonkeyPatch) -> None:
    """On the TEST substrate the wipe issues a single DETACH DELETE."""
    monkeypatch.setattr(harness, "settings", SimpleNamespace(environment=Environment.TEST))
    session = _FakeSession()
    service = _service_with_session(session)

    await wipe_substrate(service, trace_id="trace-1")

    session.run.assert_awaited_once_with(WIPE_CYPHER)


@pytest.mark.asyncio
async def test_wipe_refused_off_test_substrate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wipe refuses anywhere but the FRE-375 test substrate."""
    monkeypatch.setattr(harness, "settings", SimpleNamespace(environment=Environment.PRODUCTION))
    session = _FakeSession()
    service = _service_with_session(session)

    with pytest.raises(RuntimeError, match="refused"):
        await wipe_substrate(service, trace_id="trace-2")

    session.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_wipe_requires_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A disconnected service is a programming error, not a silent no-op."""
    monkeypatch.setattr(harness, "settings", SimpleNamespace(environment=Environment.TEST))
    service = MagicMock()
    service.driver = None

    with pytest.raises(RuntimeError, match="not connected"):
        await wipe_substrate(service, trace_id="trace-3")


def _report(wipe: bool) -> RunReport:
    return RunReport(
        run_id="iso-test",
        timestamp="2026-06-27T00:00:00+00:00",
        write_mode="extract",
        embedding_backend="real",
        prod_k=5,
        k_sweep=(1, 3, 5),
        probe_set="bespoke",
        cases=(),
        wipe_between_cases=wipe,
    )


def test_run_report_stamps_isolation_in_json() -> None:
    """Isolation status is recorded in the run-report provenance (meta)."""
    import json

    meta = json.loads(render_json(_report(wipe=True)))["meta"]
    assert meta["wipe_between_cases"] is True


def test_run_report_surfaces_isolation_in_markdown() -> None:
    """A non-isolated run is visibly flagged so the numbers aren't misread."""
    assert "per-case isolation" in render_markdown(_report(wipe=True)).lower()
    warn = render_markdown(_report(wipe=False)).lower()
    assert "no per-case isolation" in warn or "⚠" in warn
