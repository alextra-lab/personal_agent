r"""I/O driver — run the real judge over the labelled corpus and score it (FRE-1286 AC-6).

Run as a module so ``scripts`` resolves as a package::

    make test-infra-up      # FRE-375: cost substrate on :5433, never production

    # dev partition — iterate on the prompt here
    uv run python -m scripts.eval.fre1286_entailment.harness --partition dev

    # held-out — scored ONCE, after the judge is frozen
    uv run python -m scripts.eval.fre1286_entailment.harness --partition heldout

    # the adjudicated figure: the whole corpus
    uv run python -m scripts.eval.fre1286_entailment.harness

    make test-infra-down

The judge routes through LiteLLM and the ADR-0065 cost gate like any other consumer, so
this needs the test substrate up and cloud credentials. It is therefore run by hand rather
than in CI — the same split ``fre1281_span_extraction`` uses, with the pure core (corpus,
metrics) fully unit-tested and only the driver needing a model.

Cases are judged concurrently, bounded, because the corpus is a few dozen sequential
round-trips otherwise and nothing here is latency-sensitive in the way a turn is.
"""

from __future__ import annotations

import os

# FRE-375: point the cost substrate at the TEST stack BEFORE importing any personal_agent
# code — ``settings`` is a cached import-time singleton, so assigning later is too late.
# The judging calls go to the real provider; what is redirected is the cost ledger they
# reserve against, which must never be production's.
_TEST_SUBSTRATE_ENV = {
    "APP_ENV": "test",
    "AGENT_NEO4J_URI": "bolt://localhost:7688",
    "AGENT_ELASTICSEARCH_URL": "http://localhost:9201",
    "AGENT_DATABASE_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
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

from scripts.eval.fre1286_entailment.corpus import (  # noqa: E402
    EntailmentCase,
    Partition,
    load_corpus,
    partitioned,
)
from scripts.eval.fre1286_entailment.metrics import render, score  # noqa: E402

MAX_CONCURRENCY = 6
"""Concurrent judge calls. Bounded so a corpus run cannot contend with live turns."""


async def _judge_case(
    case: EntailmentCase, judge: object, semaphore: asyncio.Semaphore
) -> tuple[str, str]:
    """Judge one case.

    Args:
        case: The labelled pair.
        judge: The entailment judge.
        semaphore: Concurrency bound.

    Returns:
        ``(case id, verdict)``. A failure yields ``undecided``, which the scorer counts
        rather than drops — a run must not improve as its calls fail.
    """
    async with semaphore:
        judgement = await judge.judge(case.claim, case.passage)  # type: ignore[attr-defined]
        return case.id, judgement.verdict.value


async def _run(partition: Partition | None) -> int:
    """Score the judge over one partition.

    Args:
        partition: Which partition, or None for the whole corpus.

    Returns:
        Process exit code: 0 when every preregistered bar is met.
    """
    from personal_agent.config import settings
    from personal_agent.cost_gate import CostGate, load_budget_config, set_default_gate
    from personal_agent.grounding.entailment import ModelEntailmentJudge
    from personal_agent.llm_client.factory import get_llm_client
    from personal_agent.llm_client.types import ModelRole

    # A standalone script has no application startup, so nothing has registered the cost
    # gate and every paid call refuses — as an unmetered corpus run should. Registering it
    # here is what makes the budget lane actually bind. It reserves against the TEST
    # substrate redirected at the top of this module, never production's ledger.
    gate = CostGate(config=load_budget_config(), db_url=settings.database_url)
    await gate.connect()
    set_default_gate(gate)

    cases = partitioned(load_corpus(), partition)
    judge = ModelEntailmentJudge(
        get_llm_client(role_name=ModelRole.ENTAILMENT.value),
        timeout_s=settings.grounding_entailment_latency_budget_ms / 1000,
        max_excerpt_chars=settings.grounding_entailment_max_excerpt_chars,
    )
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    results = await asyncio.gather(
        *(_judge_case(case, judge, semaphore) for case in cases), return_exceptions=True
    )
    # A raised call contributes no entry, and the scorer reads its absence as `undecided`
    # rather than shrinking the denominator — a run must not improve as its calls fail.
    verdicts: dict[str, str] = {}
    for result in results:
        if isinstance(result, BaseException):
            print(f"  ERROR {type(result).__name__}: {result}")  # noqa: T201
            continue
        case_id, verdict = result
        verdicts[case_id] = verdict

    report = score(cases, verdicts)
    label = partition.value if partition is not None else "full"
    print(f"FRE-1286 entailment judge — partition: {label}")  # noqa: T201
    print(render(report))  # noqa: T201
    for case in cases:
        got = verdicts.get(case.id, "undecided")
        if got != case.expected:
            print(f"  MISS {case.id:<24} expected={case.expected:<14} got={got}")  # noqa: T201

    return 0 if all(met for _, _, met in report.bar_results().values()) else 1


def main() -> int:
    """Parse arguments and run.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--partition",
        choices=[member.value for member in Partition],
        default=None,
        help="dev to iterate, heldout to score once. Omit for the whole corpus.",
    )
    args = parser.parse_args()
    partition = Partition(args.partition) if args.partition else None
    return asyncio.run(_run(partition))


if __name__ == "__main__":
    raise SystemExit(main())
