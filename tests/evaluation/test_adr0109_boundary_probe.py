"""FRE-790 — ADR-0109 boundary-probe runner (fixture loading + dry-run plumbing).

Pure, cost-free tests for ``adr0109_boundary_probe`` — the committed runner FRE-782
named but never checked in. It reads a boundary fixture YAML into the same
``EntityItem`` shape ``relabel_v2_types`` classifies, so the two probes share one
blind-classification instrument. These tests exercise the fixture→items→report
plumbing with stubbed raters (``dry_run=True``) — no API calls, no cost.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from scripts.eval.fre630_extraction_quality import adr0109_boundary_probe as probe
from scripts.eval.fre630_extraction_quality.relabel_v2_types import (
    RATERS,
    build_report,
    classify_all,
)

FRE790_FIXTURE = Path(
    "scripts/eval/fre630_extraction_quality/fre790_phenomenon_domain_boundary_fixture.yaml"
)


def test_load_probe_fixture_returns_all_entities_with_context() -> None:
    """Every fixture entity becomes one EntityItem with its context populated."""
    items = probe.load_probe_fixture(FRE790_FIXTURE)

    assert len(items) == 24
    # Item ids are unique and derive from the entity name (blind — no type shown).
    assert len({item.item_id for item in items}) == 24
    # Context is carried verbatim, non-empty, for every entity (the rater sees it).
    for item in items:
        assert item.entity_name
        assert item.context.strip()
    # The ADR-named boundary case is present with its relativity context.
    spacetime = next(item for item in items if item.entity_name == "Spacetime")
    assert "spacetime" in spacetime.context.lower()


def test_dry_run_plumbing_builds_report_over_all_items() -> None:
    """Fixture → items → classify_all(dry_run) → build_report yields n_items == 24.

    Proves the whole plumbing without a paid call: the dry-run stub returns a
    valid label for every (item, rater) pair, so every item is "complete" and
    the report is computed over all 24.
    """
    items = probe.load_probe_fixture(FRE790_FIXTURE)
    by_item = asyncio.run(classify_all(items, dry_run=True))

    # Every item got a response from every rater.
    assert set(by_item) == {item.item_id for item in items}
    for responses in by_item.values():
        assert set(responses) == {r.name for r in RATERS}

    report = build_report(items, by_item)
    assert report.overall.n_items == 24


def test_load_probe_fixture_missing_file_raises() -> None:
    """A missing fixture path is a hard error, not a silent empty probe."""
    try:
        probe.load_probe_fixture(Path("scripts/eval/fre630_extraction_quality/does_not_exist.yaml"))
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError for a missing fixture path")
