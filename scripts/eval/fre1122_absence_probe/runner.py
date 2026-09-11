#!/usr/bin/env python3
"""FRE-1122 — the absence-probe runner.

Four phases, deliberately separate commands rather than one pass, because the
middle one is the only irreversible thing here and it must not be reachable by
accident:

``preflight``   Establish every probe's status by query and record the evidence
                (AC-1, AC-2). Replaces an absent probe whose query returns rows
                with one from the pre-registered pool. **Fires no turns.**
``run``         Fire the twenty turns and classify the answers (AC-4, AC-5).
                One session per probe. Requires explicit authorization and an
                explicit identity — see below.
``postcheck``   Measure what the run created, apply run-scoped cleanup across
                both substrates, and re-check whether the absent subjects
                returned to zero rows (AC-3). This decides AC-6's substrate
                branch.
``report``      Assemble the six-cell report from the artifacts above.

**The run phase needs the owner's authorization and will not proceed without
it.** It fires real turns against the live gateway under the owner's identity;
that is not a session's to start unprompted, and ``--authorized-by`` is required
precisely so it cannot happen as a side effect of running the other phases.

**It also needs an explicit identity.** ``--auth-email`` is sent as
``Cf-Access-Authenticated-User-Email`` and must resolve to ``--user-id``. The
service upserts an unknown address into ``users``, so a run under the wrong one
measures an empty corpus and reports a clean, meaningless baseline.

**A failed probe does not discard the completed ones**, and a resume never
refires a probe whose request was submitted: the turn may have completed
server-side, and the absent half is single-use.

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
import datetime as dt
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
from scripts.eval.fre1122_absence_probe.manifest import (
    Manifest,
    ManifestError,
    load_manifest,
    write_manifest,
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

# One turn at a time. Each probe now fires in its own session, so concurrency is
# available — but the corpus is mutated by every turn, and sequential firing
# keeps what each probe was asked against deterministic.
#
# The ceiling was 300 seconds and a legitimate five-tool-iteration turn took
# 318.8, taking nine completed turns down with it. Three times the observed
# maximum, and overridable, so the ceiling moves without a code edit.
_DEFAULT_TURN_TIMEOUT_SECONDS = 900.0

# How a probe's attempt ended. The distinction that matters is whether the
# request reached the service: absent probes are single-use, so refiring one that
# may have completed server-side answers against a corpus containing its own
# first firing, which destroys the ground truth rather than repeating it.
STATE_COMPLETED = "completed"
STATE_NOT_SUBMITTED = "not_submitted"
STATE_SUBMITTED_UNKNOWN = "submitted_unknown"


class RunRefused(RuntimeError):
    """The run phase refused to fire, for a reason that would void the baseline."""


# A capture that could not be read is NOT the same as a turn that rendered no
# memory. "Zero items admitted" is a legitimate, and for FRE-1118 an
# interesting, result; a missing capture is a gap in the evidence. Collapsing
# both to an empty list made AC-5 unprovable in one direction and over-strict in
# the other (Codex round 3).
_CAPTURE_MISSING = "(capture missing — memory items for this turn are unknown)"

# ADR-0148 D2 (FRE-1478): the same "capture missing is not a fact about the turn"
# rule applies to the rendered memory state — a probe with no capture must read as
# unknown, never as inferred from ``rendered_memory`` being empty (AC-7's own bar).
_MEMORY_STATE_UNKNOWN = "unknown (capture missing)"


@dataclass(frozen=True)
class ProbeAnswer:
    """One fired probe and how its answer was classified.

    Attributes:
        probe_id: The probe.
        status: Its construction-time ground truth.
        question: What was asked.
        answer: The rendered answer, verbatim.
        session_id: The session this probe fired in. One per probe, so that no
            probe is answered inside another's conversation history.
        trace_id: Join key to the turn's capture, for AC-5's memory items.
        outcome: The classification.
        evidence_span: The verbatim span that decided it.
        reason: Why that outcome.
        rendered_memory: Memory items rendered on this turn, from the capture's
            ADR-0125 D3 recall-admission record. Empty when no capture was found.
        memory_state: The rendered ``MemoryStatus`` this turn's memory section
            carried — ``populated``, ``nothing_relevant``, ``withheld`` or
            ``unavailable`` (ADR-0148 D2, FRE-1478) — read from the same
            recall-admission record ``rendered_memory`` reads, never inferred from
            its item count. ``_MEMORY_STATE_UNKNOWN`` when no capture was found.
    """

    probe_id: str
    status: str
    question: str
    answer: str
    session_id: str
    trace_id: str
    outcome: str
    evidence_span: str
    reason: str
    rendered_memory: tuple[str, ...]
    memory_state: str


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


def _read_capture(captures_root: pathlib.Path, trace_id: str) -> dict[str, object] | None:
    """Read one turn's capture JSON, the shared source for AC-5 and AC-7.

    Args:
        captures_root: Root of the on-disk capture tree.
        trace_id: The turn's trace id.

    Returns:
        The parsed capture, or None when it is missing or unreadable.
    """
    matches = list(captures_root.glob(f"*/{trace_id}.json"))
    if not matches:
        log.warning("fre1122_capture_missing", trace_id=trace_id)
        return None

    try:
        return json.loads(matches[0].read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("fre1122_capture_unreadable", trace_id=trace_id, error=str(exc))
        return None


def _load_rendered_memory(capture: dict[str, object] | None) -> tuple[str, ...]:
    """Read the memory items rendered on a turn from its capture (AC-5).

    The ADR-0125 D3 recall-admission record names which memory items the turn
    actually relied on, by identity and score — including the ones trimming or
    rendering dropped. That is exactly what AC-5 needs to trace a confabulation
    back to what it was built from.

    Args:
        capture: The turn's parsed capture, or None when it could not be read.

    Returns:
        Item identities as strings; ``_CAPTURE_MISSING`` if no capture or no
        record was found.
    """
    if capture is None:
        return (_CAPTURE_MISSING,)

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


def _load_memory_state(capture: dict[str, object] | None) -> str:
    """Read the rendered ``MemoryStatus`` a turn's memory section carried (AC-7).

    Read from the same ADR-0125 D3 recall-admission record ``_load_rendered_memory``
    reads — ADR-0148 D2/FRE-1478 added ``memory_state`` to that record — never
    inferred from ``rendered_memory``'s item count, which is exactly the collapse
    ADR-0148 exists to end (a ``NOTHING_RELEVANT`` turn and an ``UNAVAILABLE`` one
    both render an empty item list).

    Args:
        capture: The turn's parsed capture, or None when it could not be read.

    Returns:
        The recorded state, or ``_MEMORY_STATE_UNKNOWN`` if no capture or no
        record was found.
    """
    if capture is None:
        return _MEMORY_STATE_UNKNOWN

    admission = capture.get("recall_admission") or {}
    state = admission.get("memory_state")
    return str(state) if state else _MEMORY_STATE_UNKNOWN


async def _open_pg() -> Connection:
    """Open an asyncpg connection to the configured database.

    Returns:
        An open asyncpg connection.
    """
    import asyncpg  # noqa: PLC0415 — runtime-only dependency

    url = str(settings.database_url).replace("postgresql+asyncpg://", "postgresql://")
    return await asyncpg.connect(url)


async def _verify_run_identity(pg_conn: Connection, *, email: str, user_id: str) -> None:
    """Refuse unless the run's turn identity is the owner the ground truth used.

    ``get_request_user`` **upserts** an unknown address into ``users`` and hands
    back a fresh id, so a run under the wrong email creates a user, measures
    their empty corpus, reads every present probe as absent, and reports a clean
    baseline. Nothing else catches that, and nothing about the report looks
    wrong.

    Args:
        pg_conn: An open asyncpg connection.
        email: The address the run will send as the authenticated user.
        user_id: The owner the ground-truth queries were scoped to.

    Raises:
        RunRefused: If the address is unknown, or maps to a different user.
    """
    row = await pg_conn.fetchrow("SELECT user_id FROM users WHERE email = lower($1)", email)
    if row is None:
        raise RunRefused(
            f"refusing to run: {email} is not a known user. The service would "
            "create it on the first turn and the run would measure an empty "
            "corpus while reporting a clean baseline."
        )
    if str(row["user_id"]) != str(user_id):
        raise RunRefused(
            f"refusing to run: {email} resolves to {row['user_id']}, but the "
            f"ground truth was established for {user_id}. The run would measure a "
            "different corpus from the one preflight evidenced."
        )


def _is_refirable(attempt: dict[str, object] | None) -> bool:
    """Whether a probe may be fired, given what an earlier attempt recorded.

    Args:
        attempt: The ledger entry for this probe, or None if it has none.

    Returns:
        True for a probe never attempted, or one whose request provably never
        reached the service. A probe that was submitted is never refired: the
        turn may have completed server-side, and the absent half is single-use.
    """
    if attempt is None:
        return True
    return attempt.get("state") == STATE_NOT_SUBMITTED


def _load_run_state(
    path: pathlib.Path,
    manifest: Manifest,
    *,
    resume: bool,
) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    """Load an existing run artifact for a resume, or refuse to overwrite one.

    Args:
        path: The run artifact.
        manifest: The effective manifest for this run.
        resume: Whether the caller asked to continue an interrupted run.

    Returns:
        The answers collected so far and the attempt ledger, both empty on a
        fresh run.

    Raises:
        RunRefused: If answers already exist and no resume was requested.
        ManifestError: If the existing artifact belongs to a different probe set.
    """
    if not path.exists():
        return [], {}

    artifact = json.loads(path.read_text())
    answers = list(artifact.get("answers") or [])
    attempts = dict(artifact.get("attempts") or {})

    if not resume:
        if answers or attempts:
            raise RunRefused(
                f"refusing to run: {path} already holds {len(answers)} answer(s). "
                "The absent probes are single-use — refiring one answers against a "
                "corpus that now contains its own first firing. Pass --resume to "
                "fire only what is missing, or move the artifact aside."
            )
        return [], {}

    if artifact.get("manifest_digest") != manifest.digest:
        raise ManifestError(
            "run_answers.json was produced against a different manifest "
            f"({str(artifact.get('manifest_digest'))[:12]} != {manifest.digest[:12]}) "
            "— resuming would mix two probe sets into one baseline"
        )
    return answers, attempts


def _write_run_state(
    path: pathlib.Path,
    manifest: Manifest,
    args: argparse.Namespace,
    answers: list[dict[str, object]],
    attempts: dict[str, dict[str, object]],
) -> None:
    """Rewrite the run artifact and its cleanup-compatibility shape.

    Called after every probe rather than once at the end. A run that aborts must
    leave both the answers it collected and the record of what it attempted;
    losing nine completed turns to one slow turn is the behaviour this replaces.

    Args:
        path: The run artifact.
        manifest: The effective manifest for this run.
        args: Parsed CLI arguments.
        answers: The answers collected so far.
        attempts: The attempt ledger.
    """
    session_ids = list(dict.fromkeys(str(a["session_id"]) for a in answers if a.get("session_id")))
    path.write_text(
        json.dumps(
            {
                "session_ids": session_ids,
                "authorized_by": args.authorized_by.strip(),
                "auth_email": args.auth_email,
                "manifest_digest": manifest.digest,
                "attempts": attempts,
                "answers": answers,
            },
            indent=2,
            default=str,
        )
    )

    # Compatibility shape for scripts/cleanup_eval_data.py, which purges the
    # relational and Elasticsearch side and consumes an A/B results.json.
    # FRE-1122 is single-arm, so every turn is a control side. Each row names its
    # OWN probe's session — with one session per probe, a shared outer id would
    # have named the last probe's session on every row.
    compat = _artifact(args.artifact_root, "results.json")
    compat.write_text(
        json.dumps(
            [
                {"control": {"session_id": a.get("session_id"), "trace_id": a.get("trace_id")}}
                for a in answers
            ],
            indent=2,
        )
    )


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
    effective: list[Probe] = []
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
            effective.append(current)
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

    # The effective probe list — after replacement — is what every later phase
    # binds to. Without it, run/postcheck/report each re-read the original YAML
    # and a replaced probe silently comes back (Codex round 1, finding 2).
    manifest = write_manifest(
        args.artifact_root,
        probes=tuple(effective),
        user_id=args.user_id,
        probe_set_path=args.probe_set,
        ground_truth_holds=failures == 0,
        replacements=tuple(replacements),
        created_at=dt.datetime.now(dt.UTC).isoformat(),
    )
    log.info(
        "fre1122_preflight_written",
        path=str(path),
        failures=failures,
        manifest_digest=manifest.digest[:12],
    )
    return 1 if failures else 0


async def _phase_run(args: argparse.Namespace, probe_set: ProbeSet) -> int:
    """Fire the twenty turns and classify each answer.

    Fires the **manifest's** probes, not the YAML's. Preflight may have replaced
    an absent probe whose subject turned out to be present; re-reading the source
    file here would fire the original and label a present subject absent.

    Args:
        args: Parsed CLI arguments, including the required authorization.
        probe_set: The loaded probe set, for the manifest's shape check.

    Returns:
        Process exit code.

    Raises:
        ManifestError: If preflight has not run, failed, or described a
            different probe set or owner.
    """
    manifest = load_manifest(
        args.artifact_root,
        probe_set=probe_set,
        probe_set_path=args.probe_set,
        user_id=args.user_id,
    )
    validate_run_shape(probe_set)

    # Defence in depth: main() gates the CLI, but _phase_run is importable and
    # _dispatch calls it directly, so the live-fire phase asserts for itself
    # (Codex round 1, finding 9).
    if not (args.authorized_by or "").strip():
        raise RuntimeError(
            "_phase_run reached without authorization — this phase fires real "
            "turns at the live gateway under the owner's identity"
        )

    path = _artifact(args.artifact_root, "run_answers.json")
    answers, attempts = _load_run_state(path, manifest, resume=bool(args.resume))

    # Bind the two identities BEFORE the client opens. The ground-truth queries
    # scope to --user-id and the turn scopes to whoever the header names; if they
    # differ the run measures a corpus preflight never evidenced, and the report
    # states a clean baseline for it.
    pg_conn = await _open_pg()
    try:
        await _verify_run_identity(pg_conn, email=args.auth_email, user_id=args.user_id)
    finally:
        await pg_conn.close()

    pending = [p for p in manifest.probes if _is_refirable(attempts.get(p.probe_id))]
    ambiguous = [pid for pid, a in attempts.items() if a.get("state") == STATE_SUBMITTED_UNKNOWN]
    headers = {"Cf-Access-Authenticated-User-Email": args.auth_email}

    async with httpx.AsyncClient(timeout=args.turn_timeout) as client:
        for probe in pending:
            # Recorded, and made durable, BEFORE the request goes out. A crash
            # mid-turn must not look like a probe that never fired.
            attempts[probe.probe_id] = {
                "state": STATE_SUBMITTED_UNKNOWN,
                "started_at": dt.datetime.now(dt.UTC).isoformat(),
                "error": None,
            }
            _write_run_state(path, manifest, args, answers, attempts)

            try:
                response = await client.post(
                    f"{args.service_url}/chat",
                    params={"message": probe.question, "channel": "EVAL"},
                    headers=headers,
                )
                response.raise_for_status()
                payload = response.json()
                session_id = str(payload["session_id"])
                answer = str(payload.get("response", ""))
                trace_id = str(payload.get("trace_id", ""))
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # The request never reached the service, so the corpus is
                # untouched and this probe is safely refirable.
                attempts[probe.probe_id]["state"] = STATE_NOT_SUBMITTED
                attempts[probe.probe_id]["error"] = f"{type(exc).__name__}: {exc}"
                log.warning("fre1122_probe_not_submitted", probe_id=probe.probe_id, error=str(exc))
                _write_run_state(path, manifest, args, answers, attempts)
                continue
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                # The request went out. The turn may have completed server-side
                # and written to the corpus, so this probe is never auto-refired.
                attempts[probe.probe_id]["error"] = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "fre1122_probe_submitted_unknown", probe_id=probe.probe_id, error=str(exc)
                )
                _write_run_state(path, manifest, args, answers, attempts)
                continue

            classification = classify_answer(
                answer,
                status=probe.status,
                expected_tokens=probe.expected_tokens,
                subject_terms=probe.subject_terms,
            )
            capture = _read_capture(args.captures_root, trace_id)
            answers.append(
                asdict(
                    ProbeAnswer(
                        probe_id=probe.probe_id,
                        status=probe.status,
                        question=probe.question,
                        answer=answer,
                        session_id=session_id,
                        trace_id=trace_id,
                        outcome=str(classification.outcome),
                        evidence_span=classification.evidence_span,
                        reason=classification.reason,
                        rendered_memory=_load_rendered_memory(capture),
                        memory_state=_load_memory_state(capture),
                    )
                )
            )
            attempts[probe.probe_id] = {
                "state": STATE_COMPLETED,
                "started_at": attempts[probe.probe_id]["started_at"],
                "session_id": session_id,
                "trace_id": trace_id,
                "error": None,
            }
            _write_run_state(path, manifest, args, answers, attempts)
            log.info(
                "fre1122_probe_answered",
                probe_id=probe.probe_id,
                outcome=str(classification.outcome),
                trace_id=trace_id,
            )

    incomplete = [
        p.probe_id for p in manifest.probes if p.probe_id not in {a["probe_id"] for a in answers}
    ]
    log.info(
        "fre1122_run_written",
        path=str(path),
        answered=len(answers),
        incomplete=len(incomplete),
    )
    if ambiguous:
        log.warning(
            "fre1122_probes_need_a_decision",
            probes=sorted(ambiguous),
            reason="the request went out and no answer came back; the turn may have "
            "completed server-side, so refiring would answer against a corpus "
            "containing the probe's own first firing",
        )
    return 1 if incomplete else 0


async def _phase_postcheck(args: argparse.Namespace, probe_set: ProbeSet) -> int:
    """Measure pollution, apply cleanup, and re-check (AC-3).

    Three passes over the absent half's queries: after the run, then cleanup,
    then again. Whether the third pass returns to zero rows is what decides
    AC-6's substrate branch.

    A dry run is NOT cleanup. It reports what would be deleted and exits
    non-zero, because "cleanup applied" is the thing AC-3 asks to be
    demonstrated (Codex round 1, finding 5).

    Args:
        args: Parsed CLI arguments.
        probe_set: The loaded probe set, for the manifest's shape check.

    Returns:
        Process exit code: 0 only when cleanup actually ran and the absent half
        returned to zero rows. Unrestored residue is a real result — it selects
        AC-6's test-substrate branch — but it is not a success.
    """
    manifest = load_manifest(
        args.artifact_root,
        probe_set=probe_set,
        probe_set_path=args.probe_set,
        user_id=args.user_id,
        require_ground_truth=False,
    )
    run_artifact = json.loads((args.artifact_root / "run_answers.json").read_text())

    if run_artifact.get("manifest_digest") != manifest.digest:
        raise ManifestError(
            "run_answers.json was produced against a different manifest "
            f"({str(run_artifact.get('manifest_digest'))[:12]} != "
            f"{manifest.digest[:12]}) — the pollution measured would not be this "
            "run's"
        )

    session_ids = list(run_artifact["session_ids"])
    trace_ids = [a["trace_id"] for a in run_artifact["answers"] if a["trace_id"]]

    driver = connect_graph()
    pg_conn = await _open_pg()

    try:
        after_run = [
            asdict(await gather_evidence(driver, pg_conn, p, user_id=args.user_id))
            for p in manifest.absent_probes
        ]

        cleanup = await cleanup_probe_session(
            driver,
            session_ids,
            user_id=args.user_id,
            snapshot_path=_artifact(args.artifact_root, "cleanup_snapshot.jsonl"),
            pg_conn=pg_conn,
            trace_ids=trace_ids,
            dry_run=args.dry_run,
            restore_superseded=args.restore_superseded_claims,
        )

        after_cleanup = [
            asdict(await gather_evidence(driver, pg_conn, p, user_id=args.user_id))
            for p in manifest.absent_probes
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
                "manifest_digest": manifest.digest,
                "cleanup_executed": not args.dry_run,
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
        adopted_entities_retained=len(cleanup.adopted_entities_retained),
    )
    if args.dry_run:
        log.warning(
            "fre1122_postcheck_dry_run",
            reason="nothing was deleted; re-run with --execute to apply cleanup",
        )
        return 3
    return 0 if restored else 4


def _validate_run_artifact(manifest: Manifest, run_artifact: dict[str, object]) -> None:
    """Refuse to report unless the answers actually cover the manifest's probes.

    Without this an empty or stale artifact produced a clean "0 / 0" baseline and
    exited zero — the report asserting a rate it had no data for (Codex round 1,
    finding 6). Every check here is a way that could happen.

    Args:
        manifest: The effective manifest for this run.
        run_artifact: The parsed ``run_answers.json``.

    Raises:
        ManifestError: If the answers do not correspond, one-to-one and in full,
            to the manifest's probes.
    """
    answers = run_artifact.get("answers") or []
    if not isinstance(answers, list) or not answers:
        raise ManifestError("run_answers.json holds no answers; there is no baseline to report")

    if run_artifact.get("manifest_digest") != manifest.digest:
        raise ManifestError(
            "run_answers.json was produced against a different manifest — the "
            "report would attribute one run's answers to another run's probes"
        )

    answered = [str(a.get("probe_id")) for a in answers]
    expected = [p.probe_id for p in manifest.probes]
    if sorted(answered) != sorted(expected):
        missing = sorted(set(expected) - set(answered))
        extra = sorted(set(answered) - set(expected))
        raise ManifestError(
            f"answers do not match the manifest's probes (missing={missing}, extra={extra})"
        )
    if len(answered) != len(set(answered)):
        raise ManifestError("run_answers.json contains duplicate probe ids")

    # The artifact's own `status` field is not evidence of anything — a fabricated
    # file claiming twenty absent probes validated against itself (Codex round 2).
    # Status comes from the manifest, which preflight evidenced by query.
    by_id = {p.probe_id: p for p in manifest.probes}
    valid = {str(o) for o in Outcome}
    for answer in answers:
        probe = by_id[str(answer.get("probe_id"))]
        if str(answer.get("status")) != probe.status:
            raise ManifestError(
                f"probe {probe.probe_id} is {probe.status} in the manifest but the "
                f"answers artifact claims {answer.get('status')!r}"
            )
        if str(answer.get("question")) != probe.question:
            raise ManifestError(
                f"probe {probe.probe_id}'s recorded question does not match the "
                "manifest — the answer was produced for a different question"
            )
        if str(answer.get("outcome")) not in valid:
            raise ManifestError(
                f"probe {answer.get('probe_id')} carries an unknown outcome "
                f"{answer.get('outcome')!r} — every answer must be classified (AC-4)"
            )
        if not str(answer.get("trace_id") or "").strip():
            raise ManifestError(
                f"probe {answer.get('probe_id')} has no trace_id, so its rendered "
                "memory cannot be traced (AC-5)"
            )
        # AC-5 requires every confident assertion on an absent probe name the
        # memory items it was built from. A missing capture silently became
        # "(no capture found)" and still passed, making the provenance optional.
        if probe.status == "absent" and str(answer.get("outcome")) == Outcome.ASSERTED_WRONG:
            rendered = answer.get("rendered_memory")
            if not isinstance(rendered, list):
                raise ManifestError(
                    f"probe {probe.probe_id}: rendered_memory must be a list, got "
                    f"{type(rendered).__name__}"
                )
            if _CAPTURE_MISSING in rendered:
                raise ManifestError(
                    f"probe {probe.probe_id} confabulated on an absent subject but "
                    "its capture could not be read, so AC-5 cannot trace what the "
                    "confabulation was built from"
                )


def _phase_report(args: argparse.Namespace, probe_set: ProbeSet) -> int:
    """Assemble the six-cell report from the run artifacts (AC-4, AC-5, AC-6).

    Args:
        args: Parsed CLI arguments.
        probe_set: The loaded probe set, for the manifest's shape check.

    Returns:
        Process exit code.

    Raises:
        ManifestError: If the artifacts do not describe a complete run.
    """
    manifest = load_manifest(
        args.artifact_root,
        probe_set=probe_set,
        probe_set_path=args.probe_set,
        user_id=args.user_id,
    )
    run_artifact = json.loads((args.artifact_root / "run_answers.json").read_text())
    _validate_run_artifact(manifest, run_artifact)
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
        f"Sessions: {len(run_artifact['session_ids'])} (one per probe)",
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
        # ADR-0148 AC-7 (FRE-1478): the memory state each probe's memory section
        # carried, read from the turn's own recall-admission record — never from
        # `rendered_memory`'s item count, which cannot tell NOTHING_RELEVANT apart
        # from UNAVAILABLE (both render an empty item list).
        "| Probe | Status | Outcome | Memory State | Span | Why |",
        "|---|---|---|---|---|---|",
    ]
    for answer in answers:
        span = answer["evidence_span"].replace("|", "\\|")[:160]
        why = answer["reason"].replace("|", "\\|")[:160]
        # A pre-FRE-1478 or resumed artifact never wrote this key at all — .get()
        # here, not answer["memory_state"], so a historical artifact reports the
        # documented unknown fallback instead of crashing the whole report (master
        # bounce, PR #1131).
        memory_state = answer.get("memory_state") or _MEMORY_STATE_UNKNOWN
        lines.append(
            f"| {answer['probe_id']} | {answer['status']} | {answer['outcome']} | "
            f"{memory_state} | {span} | {why} |"
        )

    # AC-6 is a same-probe guarantee, so the report names the identifiers the
    # delta must reuse rather than merely asserting that it will.
    lines += [
        "",
        "## Same-probe re-run (AC-6)",
        "",
        f"Manifest digest: `{manifest.digest}`",
        "",
        "The FRE-1118 delta must reuse these identifiers exactly. A delta measured",
        "across two different probe sets is not a delta.",
        "",
        "| Probe | Status | Question |",
        "|---|---|---|",
    ]
    lines += [
        f"| {p.probe_id} | {p.status} | {p.question.replace('|', chr(92) + '|')} |"
        for p in manifest.probes
    ]

    postcheck_path = args.artifact_root / "postcheck.json"
    if postcheck_path.exists():
        postcheck = json.loads(postcheck_path.read_text())
        if postcheck.get("manifest_digest") != manifest.digest:
            raise ManifestError(
                "postcheck.json belongs to a different run than the answers; its "
                "residue numbers would describe another run's pollution"
            )
        cleanup = postcheck["cleanup"]
        lines += [
            "",
            f"Substrate decision: {postcheck['substrate_decision']}",
            "",
            f"- cleanup executed: {postcheck.get('cleanup_executed')}"
            + (
                "" if postcheck.get("cleanup_executed") else "  **(dry run — nothing was deleted)**"
            ),
            f"- absent half restored: {postcheck['absent_half_restored']}",
            f"- residual rows: {postcheck['residual_rows']}",
            f"- residue — mutated entities {cleanup['mutated_entities']}, "
            f"descriptions filled {cleanup['descriptions_filled']}, "
            f"descriptions rewritten {cleanup['descriptions_rewritten']}",
            f"- pre-existing owner claims the run superseded: "
            f"{cleanup['claims_superseded']} (restored to current: "
            f"{cleanup['claims_restored']})",
            f"- probe-created entities adopted by later turns, therefore retained: "
            f"{cleanup['adopted_entities_retained']}",
            f"- message-history rows removed: {cleanup['message_rows_removed']}",
        ]
    else:
        lines += [
            "",
            "**Substrate decision not yet taken** — postcheck has not run, so AC-6's",
            "branch is undecided and this baseline cannot yet be paired with a delta.",
        ]

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
        "--auth-email",
        help="Address sent as Cf-Access-Authenticated-User-Email. REQUIRED for the "
        "run phase, and it must resolve to --user-id: an unknown address is "
        "created on first use and the run would measure an empty corpus.",
    )
    parser.add_argument(
        "--turn-timeout",
        type=float,
        default=_DEFAULT_TURN_TIMEOUT_SECONDS,
        help="Per-turn ceiling in seconds. A legitimate five-iteration turn has "
        "been measured at 318.8s.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="run: fire only the probes an interrupted run did not complete. "
        "Probes whose request was submitted are never refired — the turn may "
        "have completed server-side and the absent half is single-use.",
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
    parser.add_argument(
        "--restore-superseded-claims",
        action="store_true",
        help="postcheck: also restore pre-existing owner claims the run invalidated. "
        "OFF by default — the restore assumes each such claim was current "
        "immediately before the run, which cannot be proven from the graph "
        "afterwards. Without it the run reports what it invalidated and leaves "
        "the repair to the owner.",
    )
    return parser


async def _dispatch(args: argparse.Namespace) -> int:
    """Route to the requested phase.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """
    probe_set = load_probe_set(args.probe_set)

    if args.phase == "report":
        return _phase_report(args, probe_set)

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

    if not args.user_id:
        print("--user-id is required for this phase", file=sys.stderr)  # noqa: T201 — CLI usage error
        return 2

    if args.phase == "run" and not (args.authorized_by or "").strip():
        print(  # noqa: T201 — CLI usage error, before logging is configured
            "refusing to run: --authorized-by is required.\n"
            "This phase fires twenty real turns at the live gateway under the "
            "owner's identity and permanently writes to the real corpus. It is "
            "not a session's to start unprompted.",
            file=sys.stderr,
        )
        return 2

    if args.phase == "run" and not (args.auth_email or "").strip():
        print(  # noqa: T201 — CLI usage error, before logging is configured
            "refusing to run: --auth-email is required.\n"
            "The turn runs under whatever identity this header names, and the "
            "ground truth was established for --user-id. An unknown address is "
            "created on first use, so the run would measure an empty corpus and "
            "report a clean baseline for it.",
            file=sys.stderr,
        )
        return 2

    return asyncio.run(_dispatch(args))


if __name__ == "__main__":
    raise SystemExit(main())
