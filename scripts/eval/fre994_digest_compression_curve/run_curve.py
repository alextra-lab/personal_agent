"""Driver for the FRE-994 digest compression curve.

Four modes, in the order they are meant to be run::

    --dry-run             Phase A. Sample, count, price. Zero model calls, zero dollars.
    --dump-calibration    Phase A. Write the calibration transcripts out for hand
                          reference authoring. Zero model calls.
    --phase-b             Phase B. The validity gate: generate the calibration subset at
                          one arm, extract, judge, and score all four gates against the
                          hand-authored references. Cheap, and it can end the loss
                          endpoint.
    --execute             Phase C. The run.
    --analyse             Recompute every table the write-up quotes, from a completed
                          run's own records. Free, and the reason the published figures
                          are reproducible rather than hand-derived.

``--dry-run`` prices the run on three labelled bases. The `ceiling` figure — every call
billed at its own output ceiling with the worst observed input ratio — is the only true
upper bound and the only one to compare against a cap. A product of a median input ratio,
a mean envelope and a p90 overshoot bounds nothing, which is what an earlier revision
called an upper bound.

The producer stays disabled throughout. This calls the model directly and never touches
``generate_session_digest``, so no session is marked clean and nothing is written to any
substrate. Records are written incrementally, so a budget denial is a loud, resumable
stop rather than a silently thinned sample.
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

import orjson
from scripts.eval.fre994_digest_compression_curve import (  # noqa: E402
    analysis,
    arms,
    corpus,
    generate,
    scoring,
)

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

#: The arm Phase B generates on. The incumbent, so the judge is calibrated against
#: digests of the length production actually produces today.
CALIBRATION_ARM = "t250"

#: Rough output of one extraction or judging call. Scoring is a small fraction of the
#: bill; the projection's precision lives in the generation stage.
_SCORING_OUTPUT_TOKENS = 600

#: Input tokens for one coverage-judging call: one reference conclusion plus one digest.
_JUDGE_INPUT_TOKENS = 1_200

_REFERENCES = Path(__file__).parent / "references"
_OUTPUT_ROOT = Path("telemetry/fre994_curve")


def _es_client() -> Any:
    from elasticsearch import AsyncElasticsearch  # noqa: PLC0415

    return AsyncElasticsearch([get_settings().elasticsearch_url], request_timeout=30)


async def _load_sample(
    es: Any, *, n: int, seed: int
) -> tuple[corpus.Sample, dict[str, Any], dict[str, Any]]:
    """Draw the sample and read every session's captures.

    Returns:
        The sample, a map of session id to its transcript and metadata, and the frame
        statistics the draw was made over. A session the reader cannot deliver in full is
        excluded loudly rather than measured short.
    """
    response = await es.search(index=corpus.CAPTURES_INDEX, body=corpus.frame_query())
    buckets = response["aggregations"]["by_session"]["buckets"]
    eligible = corpus.eligible_sessions(buckets)
    sample = corpus.draw_sample(eligible, n=n, seed=seed)
    frame = {
        "index": corpus.CAPTURES_INDEX,
        "sessions_in_index": len(buckets),
        "eligible_sessions": len(eligible),
    }

    loaded: dict[str, Any] = {}
    for ref in sample.sessions:
        read = await corpus.read_captures(ref, es_client=es, trace_id="fre994_curve")
        if not read.captures or len(read.captures) != ref.turn_count:
            # The frame counted more turns than the reader delivered, so this transcript
            # is short by an unknown amount. Measuring loss against it would charge the
            # bound for material the generator never saw.
            loaded[ref.session_id] = {"skipped": True, "read": len(read.captures)}
            continue
        loaded[ref.session_id] = {
            "skipped": False,
            "transcript": build_prompt(read.captures),
            "ended_at": read.captures[-1].timestamp,
            "turn_count": ref.turn_count,
            "quartile": ref.quartile,
            "source": str(read.source),
        }
    return sample, loaded, frame


# ── Phase A ─────────────────────────────────────────────────────────────────


async def _dry_run(*, n: int, seed: int, arm_names: list[str], judge_arms: list[str]) -> Any:
    """Draw the sample, count every prompt, and price the run on all three bases."""
    selected = [arms.ARMS_BY_NAME[name] for name in arm_names]
    es = _es_client()
    try:
        sample, loaded, frame = await _load_sample(es, n=n, seed=seed)

        sessions: list[dict[str, Any]] = []
        per_arm_tokens: dict[str, dict[str, int]] = {
            a.name: {"calls": 0, **{f"{b}_{d}": 0 for b in arms.COST_BASES for d in ("in", "out")}}
            for a in selected
        }
        score_in = score_out = 0

        for ref in sample.sessions:
            entry = loaded[ref.session_id]
            if entry["skipped"]:
                sessions.append({"session_id": ref.session_id, "skipped": "partial read"})
                continue

            transcript_tokens = estimate_tokens(entry["transcript"])
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

            score_in += transcript_tokens
            score_out += _SCORING_OUTPUT_TOKENS
            score_in += len(judge_arms) * _JUDGE_INPUT_TOKENS
            score_out += len(judge_arms) * _SCORING_OUTPUT_TOKENS

            sessions.append(
                {
                    "session_id": ref.session_id,
                    "turn_count": ref.turn_count,
                    "quartile": ref.quartile,
                    "source": entry["source"],
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
            "frame": frame,
            "sample": _manifest(sample),
            "arms": arm_names,
            "judged_arms": judge_arms,
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


def _manifest(sample: corpus.Sample) -> dict[str, Any]:
    """The reproducible record of what was drawn.

    The seed alone does not reproduce the draw. Quartiles are assigned over the *live*
    frame, which grows as sessions are captured, so the same seed against a later frame
    draws a different sample. The id list is the reproducible artifact; the seed only
    makes the choice arbitrary rather than chosen.
    """
    ids = [s.session_id for s in sample.sessions]
    return {
        "seed": sample.seed,
        "n": len(ids),
        "calibration_n": CALIBRATION_N,
        "session_ids": ids,
        "calibration_session_ids": ids[:CALIBRATION_N],
    }


async def _dump_calibration(*, n: int, seed: int) -> Any:
    """Write the calibration transcripts out for hand reference authoring.

    Written under ``telemetry/`` and **not** into ``references/``, which is committed.
    A transcript is raw session text: it carries whatever the user said, and in this
    corpus that includes real deployment hostnames. The committed artifact is the
    hand-authored reference set — the judgement that needs auditing — and the transcripts
    behind it are reproducible from the corpus by re-running this mode.
    """
    es = _es_client()
    try:
        sample, loaded, _frame = await _load_sample(es, n=n, seed=seed)
        out_dir = _OUTPUT_ROOT / "calibration-transcripts"
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for ref in sample.sessions[:CALIBRATION_N]:
            entry = loaded[ref.session_id]
            if entry["skipped"]:
                continue
            path = out_dir / f"transcript-{ref.session_id}.txt"
            path.write_text(entry["transcript"])
            written.append(
                {"session_id": ref.session_id, "path": str(path), "turns": ref.turn_count}
            )
        return {"manifest": _manifest(sample), "written": written}
    finally:
        await es.close()


# ── Phases B and C ──────────────────────────────────────────────────────────


async def _generate_cells(
    cells: list[tuple[str, arms.Arm]],
    loaded: dict[str, Any],
    *,
    out: Any,
) -> list[generate.GenerationRecord]:
    """Run the given (session, arm) cells, writing each record as it lands."""
    records: list[generate.GenerationRecord] = []
    consecutive_errors = 0
    for session_id, arm in cells:
        entry = loaded[session_id]
        try:
            response = await generate.generate(
                arm, prompt=entry["transcript"], session_id=session_id
            )
            record = generate.classify(
                response, arm=arm, session_id=session_id, ended_at=entry["ended_at"]
            )
            consecutive_errors = 0
        except Exception as e:  # noqa: BLE001 — one failed call must not end the run
            consecutive_errors += 1
            record = generate.GenerationRecord(
                session_id=session_id,
                arm=arm.name,
                outcome="provider_error",
                rendered_tokens=None,
                within_bound=False,
                content_bearing=False,
                truncated=False,
                prompt_tokens=None,
                completion_tokens=None,
                content_tokens=None,
                structural_tokens=None,
                cost_usd=0.0,
                finish_reason=None,
                digest=None,
                error=f"{type(e).__name__}: {e}"[: generate.MAX_ERROR_CHARS],
            )
            if consecutive_errors >= generate.ABORT_AFTER_CONSECUTIVE_ERRORS:
                raise SystemExit(
                    f"aborting: {consecutive_errors} consecutive failures, last was {record.error}"
                ) from e
        records.append(record)
        out.write(json.dumps(generate.record_to_json(record), default=str) + "\n")
        out.flush()
        print(f"  {session_id[:12]}… {arm.name}: {record.outcome}", flush=True)  # noqa: T201
    return records


async def _register_cost_gate() -> Any:
    """Register the cost gate, without which no paid call is metered.

    A standalone script has no application startup, so nothing has registered the gate —
    and every paid call would refuse. Registering it here is what makes the `study` cap
    actually bind: without a gate there is no reservation, and an unmetered experiment is
    exactly what a budget lane exists to prevent.
    """
    from personal_agent.cost_gate import (  # noqa: PLC0415
        CostGate,
        load_budget_config,
        set_default_gate,
    )

    gate = CostGate(config=load_budget_config(), db_url=get_settings().database_url)
    await gate.connect()
    set_default_gate(gate)
    return gate


async def _phase_b(*, n: int, seed: int, run_dir: Path) -> Any:
    """The validity gate. Generates the calibration subset, extracts, judges, scores."""
    es = _es_client()
    gate = await _register_cost_gate()
    try:
        sample, loaded, _frame = await _load_sample(es, n=n, seed=seed)
        calibration = [
            s.session_id
            for s in sample.sessions[:CALIBRATION_N]
            if not loaded[s.session_id]["skipped"]
        ]
        arm = arms.ARMS_BY_NAME[CALIBRATION_ARM]

        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "phase-b-generations.jsonl").open("w") as out:
            records = await _generate_cells([(sid, arm) for sid in calibration], loaded, out=out)

        extractions: dict[str, Any] = {}
        for sid in calibration:
            reply = await scoring.extract_conclusions(loaded[sid]["transcript"], session_id=sid)
            extractions[sid] = {
                "raw": reply.get("content"),
                "cost_usd": float(reply.get("cost_usd") or 0.0),
            }
            print(f"  extracted {sid[:12]}…", flush=True)  # noqa: T201

        judgements: dict[str, Any] = {}
        by_session = {r.session_id: r for r in records}
        for sid in calibration:
            record = by_session.get(sid)
            if record is None or record.digest is None:
                continue
            conclusions = _conclusions(extractions[sid]["raw"])
            verdicts = []
            for conclusion in conclusions:
                reply = await scoring.judge_coverage(
                    conclusion=conclusion, digest=record.digest, session_id=sid
                )
                verdicts.append(
                    {
                        "conclusion": conclusion,
                        "reply": reply.get("content"),
                        "cost_usd": float(reply.get("cost_usd") or 0.0),
                    }
                )
            judgements[sid] = verdicts
            print(f"  judged {sid[:12]}… ({len(verdicts)} conclusions)", flush=True)  # noqa: T201

        spent = (
            sum(r.cost_usd for r in records)
            + sum(e["cost_usd"] for e in extractions.values())
            + sum(v["cost_usd"] for vs in judgements.values() for v in vs)
        )
        payload = {
            "manifest": _manifest(sample),
            "arm": CALIBRATION_ARM,
            "extractions": extractions,
            "judgements": judgements,
            "actual_cost_usd": round(spent, 4),
        }
        (run_dir / "phase-b.json").write_text(json.dumps(payload, indent=2, default=str))
        return {
            "calibration_sessions": len(calibration),
            "generations": {r.session_id[:12]: r.outcome for r in records},
            "actual_cost_usd": round(spent, 4),
            "written": str(run_dir / "phase-b.json"),
        }
    finally:
        await es.close()
        await gate.disconnect()


def _conclusions(raw: str | None) -> list[str]:
    """Pull the conclusion strings out of an extractor reply."""
    if not raw:
        return []
    try:
        parsed = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return []
    return [
        c["text"] for c in parsed.get("conclusions", []) if isinstance(c, dict) and c.get("text")
    ]


async def _execute(
    *, n: int, seed: int, arm_names: list[str], judge_arms: list[str], run_dir: Path
) -> Any:
    """Phase C. Generate every cell, judge the judged arms, and apply the rule."""
    es = _es_client()
    gate = await _register_cost_gate()
    try:
        sample, loaded, frame = await _load_sample(es, n=n, seed=seed)
        measurable = [s.session_id for s in sample.sessions if not loaded[s.session_id]["skipped"]]
        selected = [arms.ARMS_BY_NAME[name] for name in arm_names]

        # Arms interleaved per session rather than run arm-major, so provider drift or a
        # mid-run outage lands on every arm equally instead of contaminating one.
        cells = [(sid, arm) for sid in measurable for arm in selected]
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "generations.jsonl").open("w") as out:
            records = await _generate_cells(cells, loaded, out=out)

        extractions: dict[str, list[str]] = {}
        extraction_cost = 0.0
        for sid in measurable:
            reply = await scoring.extract_conclusions(loaded[sid]["transcript"], session_id=sid)
            extractions[sid] = _conclusions(reply.get("content"))
            extraction_cost += float(reply.get("cost_usd") or 0.0)
            print(f"  extracted {sid[:12]}… ({len(extractions[sid])})", flush=True)  # noqa: T201

        judge_cost = 0.0
        losses: dict[tuple[str, str], bool | None] = {}
        with (run_dir / "verdicts.jsonl").open("w") as out:
            for record in records:
                if record.arm not in judge_arms or record.digest is None:
                    continue
                conclusions = extractions.get(record.session_id, [])
                if not conclusions:
                    # No reference conclusions means nothing could be lost. Recorded as
                    # None rather than False: "nothing to lose" is not "lost nothing".
                    losses[(record.session_id, record.arm)] = None
                    continue
                verdicts = []
                for conclusion in conclusions:
                    reply = await scoring.judge_coverage(
                        conclusion=conclusion, digest=record.digest, session_id=record.session_id
                    )
                    judge_cost += float(reply.get("cost_usd") or 0.0)
                    verdicts.append(_verdict(reply.get("content")))
                    out.write(
                        json.dumps(
                            {
                                "session_id": record.session_id,
                                "arm": record.arm,
                                "conclusion": conclusion,
                                "verdict": verdicts[-1],
                            }
                        )
                        + "\n"
                    )
                    out.flush()
                losses[(record.session_id, record.arm)] = any(
                    v not in scoring.COVERED_VERDICTS for v in verdicts
                )
                print(f"  judged {record.session_id[:12]}… {record.arm}", flush=True)  # noqa: T201

        outcomes_by_arm: dict[str, list[analysis.SessionOutcome]] = {}
        for record in records:
            outcomes_by_arm.setdefault(record.arm, []).append(
                analysis.SessionOutcome(
                    session_id=record.session_id,
                    arm=record.arm,
                    lost_a_conclusion=losses.get((record.session_id, record.arm)),
                    rendered_tokens=record.rendered_tokens,
                    within_bound=record.within_bound,
                    content_bearing=record.content_bearing,
                    truncated=record.truncated,
                    outcome=record.outcome,
                )
            )

        order = [a.name for a in selected if not a.unbounded and not a.bounded_schema]
        order.sort(key=lambda name: arms.ARMS_BY_NAME[name].max_tokens)
        rows, decision = analysis.decide(
            outcomes_by_arm,
            order=order,
            reference_arm="unbounded",
            bounds={a.name: (None if a.unbounded else a.max_tokens) for a in selected},
            n_sessions=len(measurable),
        )

        spent = sum(r.cost_usd for r in records) + extraction_cost + judge_cost
        payload = {
            "frame": frame,
            "manifest": _manifest(sample),
            "measurable_sessions": len(measurable),
            "arms": arm_names,
            "judged_arms": judge_arms,
            "per_arm": [r.__dict__ for r in rows],
            "decision": decision.__dict__,
            "actual_cost_usd": round(spent, 4),
        }
        (run_dir / "results.json").write_text(json.dumps(payload, indent=2, default=str))
        return payload
    finally:
        await es.close()
        await gate.disconnect()


def _verdict(raw: str | None) -> str:
    """Pull the verdict out of a judge reply, defaulting to the conservative class."""
    if not raw:
        return "missing"
    try:
        return str(orjson.loads(raw).get("verdict", "missing"))
    except orjson.JSONDecodeError:
        return "missing"


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Phase A: sample, count, price.")
    mode.add_argument(
        "--dump-calibration", action="store_true", help="Phase A: write calibration transcripts."
    )
    mode.add_argument("--phase-b", action="store_true", help="Phase B: the validity gate.")
    mode.add_argument("--execute", action="store_true", help="Phase C: the run.")
    mode.add_argument(
        "--analyse",
        action="store_true",
        help="Recompute the write-up's tables from a completed run's records. Free.",
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
    parser.add_argument("--run-id", default="run", help="subdirectory under telemetry/fre994_curve")
    args = parser.parse_args()

    arm_names = [a.strip() for a in args.arms.split(",") if a.strip()]
    judge_arms = [a.strip() for a in args.judge_arms.split(",") if a.strip()]
    unknown = (set(arm_names) | set(judge_arms)) - set(arms.ARMS_BY_NAME)
    if unknown:
        parser.error(f"unknown arm(s): {sorted(unknown)}; known: {sorted(arms.ARMS_BY_NAME)}")
    if not set(judge_arms) <= set(arm_names):
        parser.error("every judged arm must also be generated")

    run_dir = _OUTPUT_ROOT / args.run_id
    if args.dry_run:
        report = asyncio.run(
            _dry_run(n=args.n, seed=args.seed, arm_names=arm_names, judge_arms=judge_arms)
        )
        report = {k: v for k, v in report.items() if k != "sessions"}
    elif args.dump_calibration:
        report = asyncio.run(_dump_calibration(n=args.n, seed=args.seed))
    elif args.analyse:
        path = run_dir / "generations.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        report = analysis.delivery_tables(records)
    elif args.phase_b:
        report = asyncio.run(_phase_b(n=args.n, seed=args.seed, run_dir=run_dir))
    else:
        report = asyncio.run(
            _execute(
                n=args.n,
                seed=args.seed,
                arm_names=arm_names,
                judge_arms=judge_arms,
                run_dir=run_dir,
            )
        )

    print(json.dumps(report, indent=2, default=str))  # noqa: T201 — operator-facing output
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
