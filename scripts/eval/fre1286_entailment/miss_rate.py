r"""AC-4's query — the offline arm's miss rate, per answering model (FRE-1286).

AC-4 fails if "the miss rate cannot be read without hand-computation". This is the reading.
Run it::

    uv run python -m scripts.eval.fre1286_entailment.miss_rate --days 7

Each row of the sampled offline arm lands as a ``grounding_entailment_sample`` log event
(:mod:`personal_agent.grounding.entailment_sampling`) carrying ``answering_model``,
``verdict`` and ``miss``. This aggregates them.

**``undecided`` is excluded from the denominator, not counted as a pass.** A judge that
could not answer says nothing about the answering model, and folding those samples in
would make a provider outage look like the model improving. They are reported separately
so the exclusion is visible rather than silent.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from personal_agent.config import settings


async def _aggregate(days: int) -> dict[str, dict[str, Any]]:
    """Aggregate sampled verdicts per answering model.

    Args:
        days: Look-back window.

    Returns:
        ``{model: {"samples": n, "misses": n, "undecided": n, "miss_rate": float}}``.
    """
    from elasticsearch import AsyncElasticsearch

    client = AsyncElasticsearch(settings.elasticsearch_url)
    try:
        response = await client.search(
            index=f"{settings.elasticsearch_index_prefix}-*",
            size=0,
            query={
                "bool": {
                    "filter": [
                        {"term": {"event": "grounding_entailment_sample"}},
                        {"range": {"@timestamp": {"gte": f"now-{days}d"}}},
                    ]
                }
            },
            aggs={
                "by_model": {
                    "terms": {"field": "answering_model.keyword", "size": 50},
                    "aggs": {"by_verdict": {"terms": {"field": "verdict.keyword", "size": 10}}},
                }
            },
        )
    finally:
        await client.close()

    out: dict[str, dict[str, Any]] = {}
    for bucket in response["aggregations"]["by_model"]["buckets"]:
        verdicts = {entry["key"]: entry["doc_count"] for entry in bucket["by_verdict"]["buckets"]}
        undecided = verdicts.get("undecided", 0)
        decided = bucket["doc_count"] - undecided
        misses = verdicts.get("not_supported", 0) + verdicts.get("contradicted", 0)
        out[bucket["key"]] = {
            "samples": bucket["doc_count"],
            "decided": decided,
            "misses": misses,
            "undecided": undecided,
            "miss_rate": (misses / decided) if decided else None,
            "verdicts": verdicts,
        }
    return out


def main() -> int:
    """Print the per-model miss rate.

    Returns:
        Process exit code. 1 when the window holds no samples, so an empty result is a
        loud outcome rather than an encouraging blank page.
    """
    parser = argparse.ArgumentParser(description="ADR-0138 D3(d) offline miss rate per model.")
    parser.add_argument("--days", type=int, default=7, help="Look-back window in days.")
    args = parser.parse_args()

    rows = asyncio.run(_aggregate(args.days))
    if not rows:
        print(  # noqa: T201
            f"No grounding_entailment_sample events in the last {args.days}d. Either "
            "grounding_verification_mode is off, or the sample rate is 0.0."
        )
        return 1

    print(f"{'answering model':<30} {'samples':>8} {'decided':>8} {'misses':>7} {'rate':>7}")  # noqa: T201
    for model, row in sorted(rows.items()):
        rate = "n/a" if row["miss_rate"] is None else f"{row['miss_rate']:.3f}"
        print(  # noqa: T201
            f"{model:<30} {row['samples']:>8} {row['decided']:>8} {row['misses']:>7} {rate:>7}"
        )
        if row["undecided"]:
            print(  # noqa: T201
                f"{'':<30} {row['undecided']} sample(s) undecided — excluded from the rate"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
