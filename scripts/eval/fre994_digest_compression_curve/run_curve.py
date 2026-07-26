"""Driver for the FRE-994 digest compression curve.

    uv run python -m scripts.eval.fre994_digest_compression_curve.run_curve --dry-run

``--dry-run`` is **Phase A and makes zero model calls**. It draws the sample, reads
every session's captures from Elasticsearch, assembles the real prompt for every arm,
counts input tokens, corrects them to what the provider actually bills, and prices the
run per arm. That projection is what the owner authorises the spend against (AC-6).

The correction matters more than it sounds. This repo's cl100k estimator undercounts
Anthropic's billed input by about half again, and the contract's tool definition adds
1,663 tokens to every call — both measured from FRE-996's committed records. Priced
without them, this run looks a third cheaper than it is.

The paid phases are deliberately not reachable from this file yet: Phase B (the validity
gate) and Phase C (the main run) land after the projection has been seen and sized
against the ``study`` lane's standing caps.

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

#: Input tokens for one coverage-judging call: the reference item list plus one digest.
_JUDGE_INPUT_TOKENS = 1_200


def _es_client() -> Any:
    from elasticsearch import AsyncElasticsearch  # noqa: PLC0415

    return AsyncElasticsearch([get_settings().elasticsearch_url], request_timeout=30)


async def _dry_run(*, n: int, seed: int, arm_names: list[str], judge_arms: list[str]) -> Any:
    """Draw the sample, count every prompt, and price the run per arm."""
    selected = [arms.ARMS_BY_NAME[name] for name in arm_names]
    es = _es_client()
    try:
        response = await es.search(index=corpus.CAPTURES_INDEX, body=corpus.frame_query())
        buckets = response["aggregations"]["by_session"]["buckets"]
        eligible = corpus.eligible_sessions(buckets)
        sample = corpus.draw_sample(eligible, n=n, seed=seed)

        sessions: list[dict[str, Any]] = []
        per_arm_tokens = {a.name: {"input": 0, "output": 0, "calls": 0} for a in selected}
        score_in = score_out = 0

        for ref in sample.sessions:
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

            transcript_tokens = estimate_tokens(build_prompt(read.captures))

            per_arm: dict[str, dict[str, int]] = {}
            for arm in selected:
                estimated = transcript_tokens + estimate_tokens(arms.system_prompt_for(arm))
                billed_in = arms.projected_input_tokens(estimated)
                billed_out = arms.projected_output_tokens(arm)
                per_arm[arm.name] = {"input_tokens": billed_in, "output_tokens": billed_out}
                per_arm_tokens[arm.name]["input"] += billed_in
                per_arm_tokens[arm.name]["output"] += billed_out
                per_arm_tokens[arm.name]["calls"] += 1

            # Extraction: one call per session over the transcript, on the scoring model.
            score_in += transcript_tokens
            score_out += _SCORING_OUTPUT_TOKENS
            # Coverage judging: one call per judged arm, reference plus digest only.
            score_in += len(judge_arms) * _JUDGE_INPUT_TOKENS
            score_out += len(judge_arms) * _SCORING_OUTPUT_TOKENS

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

        gen_in = sum(t["input"] for t in per_arm_tokens.values())
        gen_out = sum(t["output"] for t in per_arm_tokens.values())
        projection = arms.project_cost(
            generation_input_tokens=gen_in,
            generation_output_tokens=gen_out,
            scoring_input_tokens=score_in,
            scoring_output_tokens=score_out,
        )
        arm_costs = {
            name: round(
                arms.project_cost(
                    generation_input_tokens=t["input"],
                    generation_output_tokens=t["output"],
                    scoring_input_tokens=0,
                    scoring_output_tokens=0,
                ).generation_usd,
                2,
            )
            for name, t in per_arm_tokens.items()
        }

        return {
            "frame": {
                "buckets_returned": len(buckets),
                "eligible_sessions": len(eligible),
                "index": corpus.CAPTURES_INDEX,
            },
            "sample": {"seed": seed, "n": len(sample.sessions), "calibration_n": CALIBRATION_N},
            "arms": arm_names,
            "judged_arms": judge_arms,
            # Surfaced, never silent: a session the reader could not deliver is a
            # session the curve was not measured over, and a sample size reported
            # without it is a sample size that was not achieved.
            "unreadable_sessions": [s["session_id"] for s in sessions if "skipped" in s],
            "measurable_sessions": sum(1 for s in sessions if "skipped" not in s),
            "generation_calls": sum(t["calls"] for t in per_arm_tokens.values()),
            "tokens": {
                "generation_input_billed": gen_in,
                "generation_output_upper_bound": gen_out,
                "scoring_input": score_in,
                "scoring_output": score_out,
            },
            "projection_usd": {
                "generation": round(projection.generation_usd, 2),
                "scoring": round(projection.scoring_usd, 2),
                "total_upper_bound": round(projection.total_usd, 2),
                "per_arm_generation": arm_costs,
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
    parser.add_argument("--n", type=int, default=36, help="sessions in the sample")
    parser.add_argument("--seed", type=int, default=994)
    parser.add_argument(
        "--arms",
        default=",".join(a.name for a in arms.ARMS),
        help="comma-separated arm names to generate",
    )
    parser.add_argument(
        "--judge-arms",
        default="t120,t180,t250,unbounded",
        help=(
            "arms whose digests are scored for consequential-conclusion loss. Length, "
            "delivery and completion are read off every generated arm for free; only "
            "the loss endpoint costs a judging call, so it is spent on the arms the "
            "decision rule actually reads."
        ),
    )
    args = parser.parse_args()

    if not args.dry_run:
        parser.error("only --dry-run is implemented; the paid phases follow owner sign-off")

    arm_names = [a.strip() for a in args.arms.split(",") if a.strip()]
    judge_arms = [a.strip() for a in args.judge_arms.split(",") if a.strip()]
    unknown = (set(arm_names) | set(judge_arms)) - set(arms.ARMS_BY_NAME)
    if unknown:
        parser.error(f"unknown arm(s): {sorted(unknown)}; known: {sorted(arms.ARMS_BY_NAME)}")
    if not set(judge_arms) <= set(arm_names):
        parser.error("every judged arm must also be generated")

    report = asyncio.run(
        _dry_run(n=args.n, seed=args.seed, arm_names=arm_names, judge_arms=judge_arms)
    )
    summary = {k: v for k, v in report.items() if k != "sessions"}
    print(json.dumps(summary, indent=2))  # noqa: T201 — operator-facing script output
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
