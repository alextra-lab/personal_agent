#!/usr/bin/env python3
"""FRE-1122 — the absence-probe runner.

Four phases, deliberately separate commands rather than one pass, because the
middle one is the only irreversible thing here and it must not be reachable by
accident:

``preflight``   Establish every probe's status by query and record the evidence
                (AC-1, AC-2). Replaces an absent probe whose query returns rows
                with one from the pre-registered pool. **Fires no turns.**
``run``         Fire the twenty turns and classify the answers (AC-4, AC-5).
                Requires explicit authorization — see below.
``postcheck``   Measure what the run created, apply session-scoped cleanup, and
                re-check whether the absent subjects returned to zero rows
                (AC-3). This is what decides AC-6's substrate branch.
``report``      Assemble the six-cell report from the artifacts above.

**The run phase needs the owner's authorization and will not proceed without
it.** It fires real turns against the live gateway under the owner's identity;
that is not a session's to start unprompted, and ``--authorized-by`` is required
precisely so it cannot happen as a side effect of running the other phases.

**Artifacts are gitignored, and that is deliberate.** AC-2 requires quoting the
stored text a correct answer must reproduce, and AC-7 requires probe subjects be
personally scoped to the owner. Both put real personal content in the probe set
and the report — and this repository is public. So the committed file is
``probe_set.template.yaml`` (construction rules and non-personal worked
examples); the real set and every run artifact live under
``telemetry/evaluation/fre1122-absence-probe/``, following the FRE-435
precedent that raw runs are never committed, only curated summaries.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

import httpx
import structlog
from scripts.eval.fre1122_absence_probe.classify import Outcome, classify_answer
from scripts.eval.fre1122_absence_probe.ground_truth import (
    cleanup_probe_session,
    connect_graph,
    gather_evidence,
)
from scripts.eval.fre1122_absence_probe.probes import (
    Probe,
    ProbeSet,
    load_probe_set,
    validate_run_shape,
)

from personal_agent.config import settings

if TYPE_CHECKING:
    from asyncpg import Connection

log = structlog.get_logger(__name__)

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
_DEFAULT_ARTIFACT_ROOT = _PROJECT_ROOT / "telemetry" / "evaluation" / "fre1122-absence-probe"
_DEFAULT_CAPTURES_ROOT = _PROJECT_ROOT / "telemetry" / "captains_log" / "captures"

# One turn at a time: the probes share a session and the corpus is mutated by
# every turn, so concurrency would make the ground truth of probe N depend on
# whether probe N-1 had finished writing.
_TURN_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class ProbeAnswer:
    """One fired probe and how its answer was classified.

    Attributes:
        probe_id: The probe.
        status: Its construction-time ground truth.
        question: What was asked.
        answer: The rendered answer, verbatim.
        trace_id: Join key to the turn's capture, for AC-5's memory items.
        outcome: The classification.
        evidence_span: The verbatim span that decided it.
        reason: Why that outcome.
        rendered_memory: Memory items rendered on this turn, from the capture's
            ADR-0125 D3 recall-admission record. Empty when no capture was found.
    """

    probe_id: str
    status: str
    question: str
    answer: str
    trace_id: str
    outcome: str
    evidence_span: str
    reason: str
    rendered_memory: tuple[str, ...]


def _artifact(root: pathlib.Path, name: str) -> pathlib.Path:
    """Resolve an artifact path under the run root, creating the directory.

    Args:
        root: The artifact root for this run.
        name: File name.

    Returns:
        The resolved path, with its parent created.
    """
    root.mkdir(parents=True, exist_ok=True)
    return root / name


def _load_rendered_memory(captures_root: pathlib.Path, trace_id: str) -> tuple[str, ...]:
    """Read the memory items rendered on a turn from its capture (AC-5).

    Captures are written to ``captures/YYYY-MM-DD/<trace_id>.json``, and the
    ADR-0125 D3 recall-admission record names which memory items the turn
    actually relied on, by identity and score — including the ones trimming or
    rendering dropped. That is exactly what AC-5 needs to trace a confabulation
    back to what it was built from.

    Args:
        captures_root: Root of the on-disk capture tree.
        trace_id: The turn's trace id.

    Returns:
        Item identities as strings; empty if no capture or no record was found.
    """
    matches = list(captures_root.glob(f"*/{trace_id}.json"))
    if not matches:
        log.warning("fre1122_capture_missing", trace_id=trace_id)
        return ()

    try:
        capture = json.loads(matches[0].read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("fre1122_capture_unreadable", trace_id=trace_id, error=str(exc))
        return ()

    admission = capture.get("recall_admission") or {}
    items = admission.get("items") or admission.get("admitted") or []
    rendered: list[str] = []
    for item in items:
        if isinstance(item, dict):
            identity = item.get("item_id") or item.get("identity") or item.get("name")
            score = item.get("score")
            rendered.append(f"{identity} (score={score})" if score is not None else str(identity))
        else:
            rendered.append(str(item))
    return tuple(rendered)


async def _open_pg() -> Connection:
    """Open an asyncpg connection to the configured database.

    Returns:
        An open asyncpg connection.
    """
    import asyncpg  # noqa: PLC0415 — runtime-only dependency

    url = str(settings.database_url).replace("postgresql+asyncpg://", "postgresql://")
    return await asyncpg.connect(url)


async def _phase_preflight(args: argparse.Namespace, probe_set: ProbeSet) -> int:
    """Establish every probe's status by query and record the evidence.

    An absent probe whose query returns rows is replaced from the pre-registered
    pool and the replacement is recorded with its own zero-row evidence, which is
    what AC-1 requires. A *present* probe whose query returns nothing is a
    construction error and fails the phase — it cannot be silently swapped,
    because there is no pool of substitute stored facts.

    Args:
        args: Parsed CLI arguments.
        probe_set: The loaded probe set.

    Returns:
        Process exit code: 0 when every probe's claimed status holds.
    """
    driver = connect_graph()
    pg_conn = await _open_pg()
    evidence: list[dict[str, object]] = []
    replacements: list[dict[str, str]] = []
    pool = list(probe_set.absent_pool)
    failures = 0

    try:
        for probe in probe_set.probes:
            current: Probe = probe
            bundle = await gather_evidence(driver, pg_conn, current, user_id=args.user_id)

            while not bundle.holds and current.status == "absent" and pool:
                replacement = pool.pop(0)
                log.info(
                    "fre1122_absent_probe_replaced",
                    original=current.probe_id,
                    replacement=replacement.probe_id,
                    hits=bundle.hit_count,
                )
                replacements.append(
                    {
                        "replaced": current.probe_id,
                        "replacement": replacement.probe_id,
                        "reason": f"{bundle.hit_count} row(s) returned; the subject is not absent",
                    }
                )
                evidence.append(asdict(bundle))
                current = replacement
                bundle = await gather_evidence(driver, pg_conn, current, user_id=args.user_id)

            evidence.append(asdict(bundle))
            if not bundle.holds:
                failures += 1
                log.error(
                    "fre1122_ground_truth_failed",
                    probe_id=current.probe_id,
                    expected=current.status,
                    hits=bundle.hit_count,
                )
    finally:
        await pg_conn.close()
        await driver.close()

    path = _artifact(args.artifact_root, "preflight_evidence.json")
    path.write_text(
        json.dumps(
            {"evidence": evidence, "replacements": replacements, "failures": failures},
            indent=2,
            default=str,
        )
    )
    log.info("fre1122_preflight_written", path=str(path), failures=failures)
    return 1 if failures else 0


async def _phase_run(args: argparse.Namespace, probe_set: ProbeSet) -> int:
    """Fire the twenty turns and classify each answer.

    Args:
        args: Parsed CLI arguments, including the required authorization.
        probe_set: The loaded probe set.

    Returns:
        Process exit code.
    """
    validate_run_shape(probe_set)

    answers: list[ProbeAnswer] = []
    session_id: str | None = None

    async with httpx.AsyncClient(timeout=_TURN_TIMEOUT_SECONDS) as client:
        for probe in probe_set.probes:
            params = {"message": probe.question, "channel": "EVAL"}
            if session_id:
                params["session_id"] = session_id

            response = await client.post(f"{args.service_url}/chat", params=params)
            response.raise_for_status()
            payload = response.json()

            session_id = payload["session_id"]
            answer = payload.get("response", "")
            trace_id = payload.get("trace_id", "")

            classification = classify_answer(
                answer, status=probe.status, expected_tokens=probe.expected_tokens
            )
            answers.append(
                ProbeAnswer(
                    probe_id=probe.probe_id,
                    status=probe.status,
                    question=probe.question,
                    answer=answer,
                    trace_id=trace_id,
                    outcome=str(classification.outcome),
                    evidence_span=classification.evidence_span,
                    reason=classification.reason,
                    rendered_memory=_load_rendered_memory(args.captures_root, trace_id),
                )
            )
            log.info(
                "fre1122_probe_answered",
                probe_id=probe.probe_id,
                outcome=str(classification.outcome),
                trace_id=trace_id,
            )

    path = _artifact(args.artifact_root, "run_answers.json")
    path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "authorized_by": args.authorized_by,
                "answers": [asdict(a) for a in answers],
            },
            indent=2,
            default=str,
        )
    )

    # Compatibility shape for scripts/cleanup_eval_data.py, which purges the
    # relational and Elasticsearch side by session_id and consumes an A/B
    # results.json. FRE-1122 is single-arm, so every turn is a control side.
    compat = _artifact(args.artifact_root, "results.json")
    compat.write_text(
        json.dumps(
            [{"control": {"session_id": session_id, "trace_id": a.trace_id}} for a in answers],
            indent=2,
        )
    )

    log.info("fre1122_run_written", path=str(path), session_id=session_id)
    return 0


async def _phase_postcheck(args: argparse.Namespace, probe_set: ProbeSet) -> int:
    """Measure pollution, apply cleanup, and re-check (AC-3).

    Three passes over the absent half's queries: after the run, then cleanup,
    then again. Whether the third pass returns to zero rows is what decides
    AC-6's substrate branch.

    Args:
        args: Parsed CLI arguments.
        probe_set: The loaded probe set.

    Returns:
        Process exit code.
    """
    run_artifact = json.loads((args.artifact_root / "run_answers.json").read_text())
    session_id = run_artifact["session_id"]

    driver = connect_graph()
    pg_conn = await _open_pg()

    try:
        after_run = [
            asdict(await gather_evidence(driver, pg_conn, p, user_id=args.user_id))
            for p in probe_set.absent_probes
        ]

        cleanup = await cleanup_probe_session(
            driver,
            session_id,
            snapshot_path=_artifact(args.artifact_root, "cleanup_snapshot.jsonl"),
            dry_run=args.dry_run,
        )

        after_cleanup = [
            asdict(await gather_evidence(driver, pg_conn, p, user_id=args.user_id))
            for p in probe_set.absent_probes
        ]
    finally:
        await pg_conn.close()
        await driver.close()

    residual = sum(b["hit_count"] for b in after_cleanup)
    restored = residual == 0

    path = _artifact(args.artifact_root, "postcheck.json")
    path.write_text(
        json.dumps(
            {
                "after_run": after_run,
                "cleanup": asdict(cleanup),
                "after_cleanup": after_cleanup,
                "absent_half_restored": restored,
                "residual_rows": residual,
                "substrate_decision": (
                    "live corpus — cleanup restored the absent half, so the "
                    "FRE-1118 delta can run on the same probes (AC-6)"
                    if restored
                    else "test substrate — cleanup left residue, so the delta "
                    "runs on the test substrate to keep the comparison same-probe (AC-6)"
                ),
            },
            indent=2,
            default=str,
        )
    )
    log.info(
        "fre1122_postcheck_written",
        path=str(path),
        restored=restored,
        residual_rows=residual,
        dry_run=args.dry_run,
    )
    return 0


def _phase_report(args: argparse.Namespace) -> int:
    """Assemble the six-cell report from the run artifacts (AC-4, AC-5, AC-6).

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """
    run_artifact = json.loads((args.artifact_root / "run_answers.json").read_text())
    answers = run_artifact["answers"]

    cells: dict[tuple[str, str], list[str]] = {}
    for answer in answers:
        cells.setdefault((answer["status"], answer["outcome"]), []).append(answer["probe_id"])

    absent = [a for a in answers if a["status"] == "absent"]
    honest = [a for a in absent if a["outcome"] == Outcome.DECLARED_ABSENCE]
    asserted = [a for a in absent if a["outcome"] == Outcome.ASSERTED_WRONG]

    lines = [
        "# FRE-1122 — absence-probe baseline",
        "",
        f"Session: {run_artifact['session_id']}",
        f"Authorized by: {run_artifact['authorized_by']}",
        "",
        "## The baseline number (AC-5)",
        "",
        f"- Honest declarations of absence: **{len(honest)} / {len(absent)}**",
        f"- Confident assertions on nothing: **{len(asserted)} / {len(absent)}**",
        "",
        "## Outcome cells",
        "",
        "| Known status | Outcome | Count | Probes |",
        "|---|---|---|---|",
    ]
    for (status, outcome), ids in sorted(cells.items()):
        lines.append(f"| {status} | {outcome} | {len(ids)} | {', '.join(ids)} |")

    lines += ["", "## Confabulation provenance (AC-5)", ""]
    if asserted:
        for answer in asserted:
            items = answer["rendered_memory"] or ["(no capture found for this turn)"]
            lines.append(f"**{answer['probe_id']}** — {answer['question']}")
            lines.append(f"> {answer['evidence_span']}")
            lines.append("")
            lines.append("Memory items rendered on this turn:")
            lines += [f"- {item}" for item in items]
            lines.append("")
    else:
        lines.append("No confident assertion on an absent probe. Nothing to trace.")
        lines.append("")

    lines += [
        "## Per-probe classification (AC-4)",
        "",
        "| Probe | Status | Outcome | Span |",
        "|---|---|---|---|",
    ]
    for answer in answers:
        span = answer["evidence_span"].replace("|", "\\|")[:160]
        lines.append(
            f"| {answer['probe_id']} | {answer['status']} | {answer['outcome']} | {span} |"
        )

    path = _artifact(args.artifact_root, "report.md")
    path.write_text("\n".join(lines) + "\n")
    log.info("fre1122_report_written", path=str(path))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase", choices=("preflight", "run", "postcheck", "report"), help="Which phase to run"
    )
    parser.add_argument(
        "--probe-set", type=pathlib.Path, required=True, help="Path to the probe-set YAML"
    )
    parser.add_argument(
        "--artifact-root",
        type=pathlib.Path,
        default=_DEFAULT_ARTIFACT_ROOT,
        help="Where run artifacts are written (gitignored)",
    )
    parser.add_argument(
        "--captures-root", type=pathlib.Path, default=_DEFAULT_CAPTURES_ROOT, help="Capture tree"
    )
    parser.add_argument("--user-id", help="Owner user UUID, for turn and message queries")
    parser.add_argument(
        "--service-url", default=f"http://localhost:{settings.service_port}", help="Agent service"
    )
    parser.add_argument(
        "--authorized-by",
        help="Who authorized this run. REQUIRED for the run phase — it fires real "
        "turns against the live gateway under the owner's identity.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="postcheck: actually delete. Omit for a dry run (the default).",
    )
    return parser


async def _dispatch(args: argparse.Namespace) -> int:
    """Route to the requested phase.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """
    if args.phase == "report":
        return _phase_report(args)

    probe_set = load_probe_set(args.probe_set)

    if args.phase == "preflight":
        return await _phase_preflight(args, probe_set)
    if args.phase == "run":
        return await _phase_run(args, probe_set)
    return await _phase_postcheck(args, probe_set)


def main() -> int:
    """CLI entry point.

    Returns:
        Process exit code.
    """
    args = _build_parser().parse_args()
    args.dry_run = not args.execute

    if args.phase in ("preflight", "run", "postcheck") and not args.user_id:
        print("--user-id is required for this phase", file=sys.stderr)  # noqa: T201 — CLI usage error
        return 2

    if args.phase == "run" and not args.authorized_by:
        print(  # noqa: T201 — CLI usage error, before logging is configured
            "refusing to run: --authorized-by is required.\n"
            "This phase fires twenty real turns at the live gateway under the "
            "owner's identity and permanently writes to the real corpus. It is "
            "not a session's to start unprompted.",
            file=sys.stderr,
        )
        return 2

    return asyncio.run(_dispatch(args))


if __name__ == "__main__":
    raise SystemExit(main())
