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

#: The precommitted sample size. Fixed before the run and not revised after seeing any
#: result: an "extend if there is budget left" rule reads as prudence but is optional
#: stopping, because spend is driven by output length and output length *is* one of the
#: two decision endpoints.
PRECOMMITTED_N = 20

#: Sessions whose reference sets are hand-authored before any digest is seen — the first
#: eight of the stratified draw, so the subset is fixed by the seed rather than chosen.
#: The only genuinely independent ground truth in the study (§4.3).
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
    """Draw the sample, count every prompt, and price the run on all three bases."""
    selected = [arms.ARMS_BY_NAME[name] for name in arm_names]
    es = _es_client()
    try:
        response = await es.search(index=corpus.CAPTURES_INDEX, body=corpus.frame_query())
        buckets = response["aggregations"]["by_session"]["buckets"]
        eligible = corpus.eligible_sessions(buckets)
        sample = corpus.draw_sample(eligible, n=n, seed=seed)

        sessions: list[dict[str, Any]] = []
        per_arm_tokens: dict[str, dict[str, int]] = {
            a.name: {"calls": 0, **{f"{b}_{d}": 0 for b in arms.COST_BASES for d in ("in", "out")}}
            for a in selected
        }
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
                per_arm[arm.name] = {"estimated_prompt_tokens": estimated}
                for basis in arms.COST_BASES:
                    billed_in = arms.projected_input_tokens(estimated, arm=arm, basis=basis)
                    billed_out = arms.projected_output_tokens(arm, basis=basis)
                    per_arm[arm.name][f"{basis}_in"] = billed_in
                    per_arm[arm.name][f"{basis}_out"] = billed_out
                    per_arm_tokens[arm.name][f"{basis}_in"] += billed_in
                    per_arm_tokens[arm.name][f"{basis}_out"] += billed_out
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

        projections: dict[str, Any] = {}
        for basis in arms.COST_BASES:
            gen_in = sum(t[f"{basis}_in"] for t in per_arm_tokens.values())
            gen_out = sum(t[f"{basis}_out"] for t in per_arm_tokens.values())
            projected = arms.project_cost(
                generation_input_tokens=gen_in,
                generation_output_tokens=gen_out,
                scoring_input_tokens=score_in,
                scoring_output_tokens=score_out,
            )
            projections[basis] = {
                "generation_usd": round(projected.generation_usd, 2),
                "scoring_usd": round(projected.scoring_usd, 2),
                "total_usd": round(projected.total_usd, 2),
                "per_arm_generation_usd": {
                    name: round(
                        arms.project_cost(
                            generation_input_tokens=t[f"{basis}_in"],
                            generation_output_tokens=t[f"{basis}_out"],
                            scoring_input_tokens=0,
                            scoring_output_tokens=0,
                        ).generation_usd,
                        2,
                    )
                    for name, t in per_arm_tokens.items()
                },
            }

        return {
            "frame": {
                "buckets_returned": len(buckets),
                "eligible_sessions": len(eligible),
                "index": corpus.CAPTURES_INDEX,
            },
            "sample": {
                "seed": seed,
                "n": len(sample.sessions),
                "calibration_n": CALIBRATION_N,
                # The seed alone does not reproduce the draw. Stratification assigns
                # quartiles over the *live* frame, which grows as sessions are captured
                # — the eligible count moved 314 → 315 between two runs half an hour
                # apart — so the same seed against a later frame draws a different
                # sample. The id list is the reproducible artifact; the seed only makes
                # it arbitrary.
                "session_ids": [s.session_id for s in sample.sessions],
                "calibration_session_ids": [s.session_id for s in sample.sessions[:CALIBRATION_N]],
            },
            "arms": arm_names,
            "judged_arms": judge_arms,
            # Surfaced, never silent: a session the reader could not deliver is a
            # session the curve was not measured over, and a sample size reported
            # without it is a sample size that was not achieved.
            "unreadable_sessions": [s["session_id"] for s in sessions if "skipped" in s],
            "measurable_sessions": sum(1 for s in sessions if "skipped" not in s),
            "generation_calls": sum(t["calls"] for t in per_arm_tokens.values()),
            "scoring_tokens": {"input": score_in, "output": score_out},
            # Three bases, never one number. `ceiling` is the only true upper bound and
            # the only one to compare against a cap; `expected` is the likely spend.
            "projection_usd": projections,
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
    # Defaults ARE the precommitted design, not a starting point to be overridden: the
    # plan's cost, multiplicity and selection claims are all computed for exactly this
    # sample size and arm set, so a default that ran anything else would price one
    # experiment and execute another.
    parser.add_argument("--n", type=int, default=PRECOMMITTED_N, help="sessions in the sample")
    parser.add_argument("--seed", type=int, default=994)
    parser.add_argument(
        "--arms",
        default=",".join(a.name for a in arms.ARMS),
        help="comma-separated arm names to generate (default: the precommitted set)",
    )
    parser.add_argument(
        "--judge-arms",
        default=",".join(arms.JUDGED_ARM_NAMES),
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
