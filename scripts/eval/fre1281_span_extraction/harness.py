"""I/O driver — run the real extractor over the corpus and score it (FRE-1281).

Run as a module so ``scripts`` resolves as a package::

    make test-infra-up      # FRE-375: cost substrate on :5433, never production

    # dev partition — per-document diffs, for iterating
    uv run python -m scripts.eval.fre1281_span_extraction.harness \\
        --run-id dev-$(date +%Y%m%d) --partition dev

    # held-out — aggregate only, scored once, after the extractor is frozen
    uv run python -m scripts.eval.fre1281_span_extraction.harness \\
        --run-id heldout-$(date +%Y%m%d) --partition heldout

    # the adjudicated figure: the whole corpus
    uv run python -m scripts.eval.fre1281_span_extraction.harness --run-id full-$(date +%Y%m%d)

    make test-infra-down

Reports land in ``telemetry/evaluation/fre1281-span-extraction/`` (gitignored — raw runs
are never committed; the numbers that matter go in the handoff and the ticket).

The extractor routes through LiteLLM and the ADR-0065 cost gate like any other consumer,
so this needs the test substrate up and cloud credentials. It is therefore run by hand,
not in CI — the same split FRE-630 uses, with the pure core fully unit-tested and only
the driver needing a model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import structlog
from scripts.eval.fre1281_span_extraction.corpus import (
    DEFAULT_CORPUS_PATH,
    GoldDocument,
    Partition,
    load_corpus,
)
from scripts.eval.fre1281_span_extraction.metrics import score_document
from scripts.eval.fre1281_span_extraction.report import aggregate, render_markdown

from personal_agent.grounding.extractor import ModelSpanExtractor
from personal_agent.grounding.spans import SpanExtraction

log = structlog.get_logger(__name__)

OUTPUT_DIR = Path("telemetry/evaluation/fre1281-span-extraction")

#: Concurrent classification calls. Modest on purpose — the provider ceiling is shared
#: with whatever else is running, and a corpus run is never the urgent thing.
CONCURRENCY = 4


async def _classify(
    extractor: ModelSpanExtractor, document: GoldDocument, semaphore: asyncio.Semaphore
) -> tuple[GoldDocument, SpanExtraction | None]:
    """Classify one document, surviving a per-document failure.

    A failed document is recorded as ``None`` and excluded from scoring rather than
    scored as zero: a transport error is not an extractor result, and averaging it in
    would understate the extractor while looking like a measurement.
    """
    async with semaphore:
        try:
            return document, await extractor.extract(
                document.text, user_message=document.user_message
            )
        except Exception as exc:  # noqa: BLE001 — one document must not end the run
            log.warning("span_extraction_document_failed", doc_id=document.doc_id, error=str(exc))
            return document, None


async def run(
    *, run_id: str, partition: Partition | None, corpus_path: Path, limit: int | None
) -> int:
    """Score the extractor over the corpus and write the report.

    Args:
        run_id: Stamped into the report and the filename.
        partition: Restrict to one partition, or ``None`` for the whole corpus.
        corpus_path: Which corpus file to load.
        limit: Score at most this many documents (smoke runs).

    Returns:
        Process exit code — nonzero when a preregistered bar was not met, so a failing
        run cannot be mistaken for a passing one by a caller that only checks status.
    """
    documents = [
        d for d in load_corpus(corpus_path) if partition is None or d.partition is partition
    ]
    if limit is not None:
        documents = documents[:limit]
    if not documents:
        raise SystemExit(f"no documents for partition {partition}")

    from personal_agent.llm_client.factory import get_llm_client_for_key  # noqa: PLC0415

    extractor = ModelSpanExtractor(get_llm_client_for_key("span_extraction"))
    semaphore = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(
        *(_classify(extractor, document, semaphore) for document in documents)
    )

    scored = [
        score_document(document, extraction.spans)
        for document, extraction in results
        if extraction is not None
    ]
    failed = [document.doc_id for document, extraction in results if extraction is None]
    degraded = sum(1 for _, extraction in results if extraction is not None and extraction.degraded)

    report = aggregate(scored, partition=partition, degraded_documents=degraded)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(
        report,
        # The held-out contract: per-document detail is never passed for that partition.
        scores=scored if partition is not Partition.HELDOUT else None,
        run_id=run_id,
        extractor="span_extraction role (config/model_roles.yaml)",
    )
    if failed:
        markdown += f"\n> {len(failed)} document(s) failed to classify and were excluded.\n"

    (OUTPUT_DIR / f"{run_id}.md").write_text(markdown, encoding="utf-8")
    (OUTPUT_DIR / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "partition": None if partition is None else partition.value,
                "documents": len(scored),
                "failed_documents": failed,
                "degraded_documents": degraded,
                "metrics": report.metrics,
                "unmet_bars": [bar.key for bar in report.unmet_bars()],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(markdown)  # noqa: T201 — this is a CLI; the report is its output
    return 0 if report.passed else 1


def main() -> int:
    """Parse arguments and run.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--partition", choices=[p.value for p in Partition], default=None)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    return asyncio.run(
        run(
            run_id=args.run_id,
            partition=Partition(args.partition) if args.partition else None,
            corpus_path=args.corpus,
            limit=args.limit,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
