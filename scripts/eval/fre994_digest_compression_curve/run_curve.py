"""Driver for the FRE-994 digest compression curve.

    uv run python -m scripts.eval.fre994_digest_compression_curve.run_curve --dry-run

``--dry-run`` is **Phase A and makes zero model calls**. It draws the sample, reads
every session's captures from Elasticsearch, assembles the real prompt for every arm,
counts input tokens exactly, and prices the run. That projection — measured, not
estimated — is what the owner authorises the spend against (AC-6).

The paid phases are deliberately not reachable from this file yet: Phase B (the
validity gate) and Phase C (the main run) land after the projection has been seen and
the ``study`` cost lane has been sized for it.

This writes nothing to any substrate. It reads Elasticsearch and prints.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # pragma: no cover — direct-script invocation
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.eval.fre994_digest_compression_curve import arms, corpus  # noqa: E402

from personal_agent.config.settings import get_settings  # noqa: E402
from personal_agent.llm_client.token_counter import estimate_tokens  # noqa: E402
from personal_agent.second_brain.session_summary import build_prompt  # noqa: E402

#: Sessions whose reference sets are hand-authored before any digest is seen. The
#: only genuinely independent ground truth in the study (§4.3).
CALIBRATION_N = 8

#: Rough output of one extraction or judging call. Scoring is a small fraction of the
#: bill; the projection's precision lives in the generation stage.
_SCORING_OUTPUT_TOKENS = 600


def _es_client() -> Any:
    from elasticsearch import AsyncElasticsearch  # noqa: PLC0415

    return AsyncElasticsearch([get_settings().elasticsearch_url], request_timeout=30)


async def _dry_run(*, fit_n: int, holdout_n: int, seed: int) -> dict[str, Any]:
    """Draw the sample, count every prompt exactly, and price the run."""
    es = _es_client()
    try:
        response = await es.search(index=corpus.CAPTURES_INDEX, body=corpus.frame_query())
        buckets = response["aggregations"]["by_session"]["buckets"]
        eligible = corpus.eligible_sessions(buckets)
        sample = corpus.draw_sample(eligible, fit_n=fit_n, holdout_n=holdout_n, seed=seed)

        all_arms = [*arms.ARMS, arms.FREE_TEXT_CONTRAST_ARM]
        sessions: list[dict[str, Any]] = []
        gen_in = gen_out = score_in = score_out = 0

        for ref in (*sample.fit, *sample.holdout):
            read = await corpus.read_captures(ref, es_client=es, trace_id="fre994_dry_run")
            if not read.captures:
                sessions.append({"session_id": ref.session_id, "skipped": "no captures read"})
                continue
            if len(read.captures) != ref.turn_count:
                # The frame counted more turns than the reader could deliver, so this
                # transcript is short by an unknown amount. Measuring loss against it
                # would charge the bound for material the generator never saw — the
                # silent truncation ADR-0124 forbids, arriving through the eval.
                sessions.append(
                    {
                        "session_id": ref.session_id,
                        "skipped": f"partial read: {len(read.captures)} of {ref.turn_count} turns",
                    }
                )
                continue

            transcript = build_prompt(read.captures)
            transcript_tokens = estimate_tokens(transcript)

            per_arm = {}
            for arm in all_arms:
                prompt_tokens = transcript_tokens + estimate_tokens(arms.system_prompt_for(arm))
                out = arms.estimate_max_output_tokens(arm)
                per_arm[arm.name] = {"input_tokens": prompt_tokens, "max_output_tokens": out}
                gen_in += prompt_tokens
                gen_out += out

            # Extraction: one call per session over the transcript.
            score_in += transcript_tokens
            score_out += _SCORING_OUTPUT_TOKENS
            # Coverage judging: one call per arm, reference + digest only.
            score_in += len(all_arms) * 1_200
            score_out += len(all_arms) * _SCORING_OUTPUT_TOKENS

            sessions.append(
                {
                    "session_id": ref.session_id,
                    "turn_count": ref.turn_count,
                    "captures_read": len(read.captures),
                    "source": str(read.source),
                    "complete": read.complete,
                    "quartile": ref.quartile,
                    "transcript_tokens": transcript_tokens,
                    "arms": per_arm,
                }
            )

        projection = arms.project_cost(
            generation_input_tokens=gen_in,
            generation_output_tokens=gen_out,
            scoring_input_tokens=score_in,
            scoring_output_tokens=score_out,
        )

        return {
            "frame": {
                "buckets_returned": len(buckets),
                "eligible_sessions": len(eligible),
                "index": corpus.CAPTURES_INDEX,
            },
            "sample": {
                "seed": seed,
                "fit_n": len(sample.fit),
                "holdout_n": len(sample.holdout),
                "calibration_n": CALIBRATION_N,
            },
            "arms": [a.name for a in all_arms],
            # Surfaced, never silent: a session the reader could not deliver is a
            # session the curve was not measured over, and a sample size reported
            # without it is a sample size that was not achieved.
            "unreadable_sessions": [s["session_id"] for s in sessions if "skipped" in s],
            "measurable_sessions": sum(1 for s in sessions if "skipped" not in s),
            "generation_calls": sum(1 for s in sessions if "skipped" not in s) * len(all_arms),
            "tokens": {
                "generation_input": gen_in,
                "generation_output_upper_bound": gen_out,
                "scoring_input": score_in,
                "scoring_output": score_out,
            },
            "projection_usd": {
                "generation": round(projection.generation_usd, 2),
                "scoring": round(projection.scoring_usd, 2),
                "total_upper_bound": round(projection.total_usd, 2),
            },
            "sessions": sessions,
        }
    finally:
        await es.close()


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Phase A: sample, count and price. Makes no model calls.",
    )
    parser.add_argument("--fit-n", type=int, default=60)
    parser.add_argument("--holdout-n", type=int, default=12)
    parser.add_argument("--seed", type=int, default=994)
    args = parser.parse_args()

    if not args.dry_run:
        parser.error("only --dry-run is implemented; the paid phases follow owner sign-off")

    report = asyncio.run(_dry_run(fit_n=args.fit_n, holdout_n=args.holdout_n, seed=args.seed))
    summary = {k: v for k, v in report.items() if k != "sessions"}
    print(json.dumps(summary, indent=2))  # noqa: T201 — operator-facing script output
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
