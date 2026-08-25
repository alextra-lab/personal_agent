r"""I/O driver — run the real extractor over the corpus and score it (FRE-1281).

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

import os

# FRE-375: point the cost substrate at the TEST stack BEFORE importing any personal_agent
# code — ``settings`` is a cached import-time singleton, so assigning later is too late.
# The classification calls go to the real provider; what is redirected is the cost ledger
# they reserve against, which must never be production's. ``setdefault`` leaves any
# caller-supplied override in place.
_TEST_SUBSTRATE_ENV = {
    "APP_ENV": "test",
    "AGENT_NEO4J_URI": "bolt://localhost:7688",
    "AGENT_ELASTICSEARCH_URL": "http://localhost:9201",
    "AGENT_DATABASE_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
    # The admin and sysgraph DSNs are separate settings and the FRE-375 startup guard
    # checks all three. Redirecting only AGENT_DATABASE_URL leaves two pointing at :5432
    # and AppConfig refuses to construct — correctly, and it is worth leaving that guard
    # loud rather than reaching for AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE.
    "AGENT_DATABASE_ADMIN_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
    "AGENT_SYSGRAPH_DATABASE_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
    "AGENT_ELASTICSEARCH_INDEX_PREFIX": "agent-logs-test",
    "AGENT_CAPTAINS_LOG_INDEX_PREFIX": "agent-captains-test",
}
for _key, _value in _TEST_SUBSTRATE_ENV.items():
    os.environ.setdefault(_key, _value)

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

import structlog  # noqa: E402
from scripts.eval.fre1281_span_extraction.corpus import (  # noqa: E402
    DEFAULT_CORPUS_PATH,
    GoldDocument,
    Partition,
    load_corpus,
)
from scripts.eval.fre1281_span_extraction.metrics import score_document  # noqa: E402
from scripts.eval.fre1281_span_extraction.report import (  # noqa: E402
    aggregate,
    render_markdown,
)

from personal_agent.grounding.extractor import ModelSpanExtractor  # noqa: E402
from personal_agent.grounding.spans import SpanExtraction  # noqa: E402

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
    *,
    run_id: str,
    partition: Partition | None,
    corpus_path: Path,
    limit: int | None,
    samples: int,
    model_key: str | None,
) -> int:
    """Score the extractor over the corpus and write the report.

    Args:
        run_id: Stamped into the report and the filename.
        partition: Restrict to one partition, or ``None`` for the whole corpus.
        corpus_path: Which corpus file to load.
        limit: Score at most this many documents (smoke runs).
        model_key: Override the deployment the ``span_extraction`` role resolves to.
            Present so "does this classifier need a stronger model?" is answerable by a
            measurement rather than an argument — AC-7 exists to inform exactly that
            choice. Overriding here rather than editing the role matrix keeps an
            experiment from leaving config churn behind.
        samples: Classify each document this many times. A single pass is a poor
            estimator here: with ~10 gold spans in a class, one span moves a per-class
            figure by 0.10, and three same-prompt dev runs during this build swung
            ``factual_entity`` across 0.50-0.80 and ``prose_in_fence`` across 0.60-1.00
            without the prompt touching either. Scoring every sample into one ratio-of-
            sums shrinks that; the per-sample spread is reported so the residual noise
            stays visible rather than being averaged out of sight.

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

    from personal_agent.config import resolve_role_model_key  # noqa: PLC0415
    from personal_agent.config.settings import get_settings  # noqa: PLC0415
    from personal_agent.cost_gate import (  # noqa: PLC0415
        CostGate,
        load_budget_config,
        set_default_gate,
    )
    from personal_agent.llm_client.factory import get_llm_client_for_key  # noqa: PLC0415

    # A standalone script has no application startup, so nothing has registered the cost
    # gate and every paid call refuses. Registering it here is what makes the budget lane
    # actually bind — an unmetered corpus run is exactly what a lane exists to prevent.
    # It reserves against the TEST substrate redirected at the top of this module.
    gate = CostGate(config=load_budget_config(), db_url=get_settings().database_url)
    await gate.connect()
    set_default_gate(gate)

    # budget_role is explicit rather than resolved through the role-name door: FRE-989
    # showed a silent default is indistinguishable from a correct mapping at every
    # downstream layer. It matches role_map.py's entry for this role.
    # The factory keys on a MODEL key, not a role name, so the role matrix resolves it
    # first (ADR-0099 D1 stage 2). budget_role is explicit rather than left to the
    # role-name door: FRE-989 showed a silent default is indistinguishable from a
    # correct mapping at every downstream layer.
    resolved_key = model_key or resolve_role_model_key("span_extraction")
    extractor = ModelSpanExtractor(
        get_llm_client_for_key(resolved_key, budget_role="entity_extraction")
    )
    semaphore = asyncio.Semaphore(CONCURRENCY)
    scored = []
    failed: list[str] = []
    degraded = 0
    per_sample_recall: list[float | None] = []

    for _sample in range(samples):
        results = await asyncio.gather(
            *(_classify(extractor, document, semaphore) for document in documents)
        )
        sample_scores = [
            score_document(document, extraction.spans)
            for document, extraction in results
            if extraction is not None
        ]
        scored.extend(sample_scores)
        failed.extend(d.doc_id for d, extraction in results if extraction is None)
        degraded += sum(1 for _, e in results if e is not None and e.degraded)
        per_sample_recall.append(
            aggregate(sample_scores, partition=partition).metrics["recall.overall"]
        )

    report = aggregate(scored, partition=partition, degraded_documents=degraded)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(
        report,
        # The held-out contract: per-document detail is never passed for that partition.
        scores=scored if partition is not Partition.HELDOUT else None,
        run_id=run_id,
        extractor=f"span_extraction role -> {resolved_key}",
    )
    spread = [r for r in per_sample_recall if r is not None]
    if samples > 1 and spread:
        markdown += (
            f"\n> {samples} samples per document. Overall recall per sample: "
            f"{', '.join(f'{r:.3f}' for r in spread)} "
            f"(min {min(spread):.3f}, max {max(spread):.3f}). The bars above are scored "
            f"over all samples pooled.\n"
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
                "samples": samples,
                "model_key": resolved_key,
                "per_sample_recall": per_sample_recall,
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
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--model-key", default=None)
    args = parser.parse_args()
    return asyncio.run(
        run(
            run_id=args.run_id,
            partition=Partition(args.partition) if args.partition else None,
            corpus_path=args.corpus,
            limit=args.limit,
            samples=args.samples,
            model_key=args.model_key,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
