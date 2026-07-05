r"""ADR-0109 boundary probe — blind 3-rater IAA over a focused boundary fixture.

The committed runner FRE-782's research note named but never checked in. It reads a
boundary fixture YAML (``{probe: [{entity, context, intended_side, boundary}, ...]}``)
into the exact ``EntityItem`` shape ``relabel_v2_types`` classifies, then **reuses that
module's blind-classification instrument unchanged** — the same 3 raters
(gpt-5.4-mini, gpt-5.4, claude-sonnet-5), the same GoLLIE-style 10-type prompt, the
same ``iaa.build_iaa_report`` statistics. Only the *source of entities* differs: a
curated boundary fixture instead of the FRE-630 gold set. This is how FRE-782 (the
KnowledgeArtifact/QuantityMeasure boundary) and FRE-790 (the Phenomenon ↔ DomainOrTopic
boundary) both run on one reproducible instrument.

Like ``relabel_v2_types``, this calls ``litellm.acompletion()`` DIRECTLY (not the app's
cost-gated ``LiteLLMClient``): there is no production extraction happening, just
single-turn blind classification — the same deliberate, documented exception.

Usage::

    # dry run — stubbed raters, no real API calls, fast smoke:
    uv run python -m scripts.eval.fre630_extraction_quality.adr0109_boundary_probe \
        --run-id smoke --dry-run

    # real run (FRE-790 Phenomenon <-> DomainOrTopic probe) — 3 real model raters:
    uv run python -m scripts.eval.fre630_extraction_quality.adr0109_boundary_probe \
        --run-id fre790-2026-07-05

Raw per-entity/per-rater records land in the gitignored
``telemetry/evaluation/fre630-extraction-quality/v2-relabel-<run-id>.json``.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import structlog
import yaml  # type: ignore[import-untyped]
from scripts.eval.fre630_extraction_quality.relabel_v2_types import (
    EntityItem,
    build_report,
    classify_all,
    render_report_table,
    write_raw_telemetry,
)

log = structlog.get_logger(__name__)

#: The FRE-790 Phenomenon <-> DomainOrTopic boundary probe (default fixture).
DEFAULT_FIXTURE = Path(
    "scripts/eval/fre630_extraction_quality/fre790_phenomenon_domain_boundary_fixture.yaml"
)


def load_probe_fixture(path: Path) -> list[EntityItem]:
    """Load a boundary fixture YAML into blind-classification entity items.

    Args:
        path: Path to a fixture file shaped ``{probe: [{entity, context, ...}]}``
            (as ``fre782_boundary_fixture.yaml`` / ``fre790_..._fixture.yaml``).
            ``intended_side`` / ``boundary`` are design metadata for the research
            note; only ``entity`` + ``context`` reach a rater (blind).

    Returns:
        One :class:`EntityItem` per probe entry, in file order. ``item_id`` is
        ``"probe::<entity>"`` and ``case_id`` is the fixture stem, so records
        never collide with gold-set relabel runs.

    Raises:
        FileNotFoundError: If ``path`` does not exist (a missing fixture is a
            hard error, never a silently empty probe).
        KeyError: If a probe entry lacks the required ``entity`` / ``context``.
    """
    if not path.exists():
        raise FileNotFoundError(f"boundary fixture not found: {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    case_id = path.stem
    items: list[EntityItem] = []
    for entry in doc["probe"]:
        entity_name = entry["entity"]
        items.append(
            EntityItem(
                item_id=f"probe::{entity_name}",
                case_id=case_id,
                entity_name=entity_name,
                context=entry["context"].strip(),
            )
        )
    return items


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE,
        help=f"Boundary fixture YAML (default: {DEFAULT_FIXTURE})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Stub raters, no real API calls")
    parser.add_argument("--limit", type=int, default=None, help="Classify only the first N items")
    args = parser.parse_args()

    items = load_probe_fixture(args.fixture)
    if args.limit:
        items = items[: args.limit]

    log.info(
        "boundary_probe_start",
        run_id=args.run_id,
        fixture=str(args.fixture),
        dry_run=args.dry_run,
        n_items=len(items),
    )
    by_item = asyncio.run(classify_all(items, dry_run=args.dry_run))
    out_path = write_raw_telemetry(args.run_id, items, by_item)
    report = build_report(items, by_item)
    print(render_report_table(report))
    log.info("boundary_probe_complete", run_id=args.run_id, out_path=str(out_path))


if __name__ == "__main__":
    main()
