#!/usr/bin/env python3
"""Deploy-verifier specialist (ADR-0113 §3, FRE-834).

The fourth judgment specialist, with two composable, independently-testable halves:

1. **Pre-deploy authorization gate** (:func:`deploy_authorized`, AC-7) — a pure, LLM-free, fail-closed
   check: only the three ADR-named **reversible** standing-approval deploy classes (``pwa``,
   ``es_template_additive``, ``kibana_import``) may proceed with no recorded owner authorization; every
   other class — the named always-ask classes *and* any unrecognized class — requires a genuine,
   verifier-accepted :class:`~scripts.specialists.harness.OwnerClearance`. This mirrors
   ``lifecycle-rules § Deploy`` ("ask first" is the default; only the three standing-approval classes
   are autonomous) as executable logic.
2. **Post-deploy outcome verification** (:func:`verify_deploy`) — the harness specialist half: gathers
   raw evidence (a health-endpoint response, the deployed git SHA) via an injected
   :class:`CommandRunner` seam, never a summary, and a fixed template
   (``.claude/agents/deploy-verifier.md``) judges pass/fail against a stated expected-SHA/healthy-
   response reference, returning a :class:`~scripts.specialists.harness.Verdict`
   (``APPROVE`` = pass, ``REJECT`` = fail) with evidence-citing findings.

**Advisory-only in FRE-834.** Neither half is wired into a real deploy pipeline call site — that is
FRE-835 (the assembled-loop seam), per ADR §5 Phase A. No ``src/`` behavior — dev-process tooling under
``scripts/``.

Callable by hand::

    python -m scripts.specialists.deploy_verifier --deploy-class gateway_rebuild --check-authorization
    python -m scripts.specialists.deploy_verifier --deploy-class pwa --verify              # dry-run
    python -m scripts.specialists.deploy_verifier --deploy-class pwa --verify --execute    # run live
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import structlog

from scripts.specialists.harness import (
    DENY_ALL_CLEARANCE,
    ClearanceVerifier,
    OwnerClearance,
    PrimaryArtifact,
    SpecialistInvocation,
    SpecialistRunner,
    Verdict,
    assemble_invocation,
    blocks_merge,
    claude_headless_runner,
    load_template,
    run_specialist,
)
from scripts.specialists.measurement_critic import REVERSIBLE_DEPLOY_CLASSES

# The fixed adversarial template this specialist runs from (harness-validated to live under
# .claude/agents/, content-versioned into every verdict).
DEPLOY_VERIFIER_TEMPLATE_PATH = Path(".claude/agents/deploy-verifier.md")

# The default post-deploy health endpoint (the agent service, per root CLAUDE.md).
_DEFAULT_HEALTH_URL = "http://localhost:9000/health"
_COMMAND_TIMEOUT_S = 30


class RunResult(Protocol):
    """The subset of ``subprocess.CompletedProcess`` the evidence-fetching seam reads."""

    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """A callable that runs an argv and returns its result (injectable seam)."""

    def __call__(self, argv: Sequence[str]) -> RunResult:
        """Run ``argv`` and return its result."""
        ...


def subprocess_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """The production :class:`CommandRunner` — a real, argv-list (no shell) subprocess call."""
    return subprocess.run(  # noqa: S603 - argv is fixed curl/git, no shell
        argv, capture_output=True, text=True, timeout=_COMMAND_TIMEOUT_S, check=False
    )


# --- AC-7: the mechanical, LLM-free pre-deploy authorization gate -----------


@dataclasses.dataclass(frozen=True)
class ProposedDeploy:
    """A deploy master may be about to actuate.

    Attributes:
        deploy_class: The deploy class (e.g. ``gateway_rebuild``, ``pwa``).
        description: Free-text context — read by nothing in the authorization gate (AC-7 is
            structured-field-only, mirroring the measurement critic's trigger).
    """

    deploy_class: str
    description: str = ""


def deploy_requires_authorization(deploy_class: str) -> bool:
    """Return whether a deploy class requires a recorded owner authorization.

    Fail-closed: **only** the three ADR-named reversible standing-approval classes bypass
    authorization. Every other class — including one this function has never seen — requires it,
    so an unrecognized deploy class cannot slip through as if it were reversible.

    Args:
        deploy_class: The deploy class under test.

    Returns:
        ``True`` unless ``deploy_class`` is a known reversible class.
    """
    return deploy_class.strip().lower() not in REVERSIBLE_DEPLOY_CLASSES


def deploy_authorized(
    deploy: ProposedDeploy,
    authorization: OwnerClearance | None = None,
    *,
    verifier: ClearanceVerifier = DENY_ALL_CLEARANCE,
) -> bool:
    """Return whether a deploy attempt may proceed (AC-7).

    Pure logic — no I/O, no LLM. A reversible-class deploy always proceeds. Every other class
    requires a genuine authorization accepted by ``verifier``; under the default
    :data:`~scripts.specialists.harness.DENY_ALL_CLEARANCE`, no always-ask (or unrecognized) deploy
    can proceed without a real verifier wired in (FRE-835 territory).

    Args:
        deploy: The proposed deploy.
        authorization: An owner-issued clearance, if any was recorded.
        verifier: The clearance verifier seam (default denies all).

    Returns:
        ``True`` iff the deploy may proceed.
    """
    if not deploy_requires_authorization(deploy.deploy_class):
        return True
    return authorization is not None and verifier(authorization)


# --- post-deploy evidence gathering + verification (runs under the harness) -


def _run_evidence(runner: CommandRunner, argv: Sequence[str]) -> str:
    """Run ``argv`` and return evidence text — a failure is captured as data, never raised."""
    result = runner(argv)
    if result.returncode != 0:
        combined = f"{result.stdout}{result.stderr}".strip()
        return f"[{' '.join(argv)}] exited {result.returncode}: {combined or '(no output)'}"
    return result.stdout.strip() or "(empty response)"


def fetch_deploy_artifact(
    deploy_class: str,
    runner: CommandRunner,
    *,
    expected_sha: str | None = None,
    health_url: str = _DEFAULT_HEALTH_URL,
) -> PrimaryArtifact:
    """Build the raw primary artifact for a post-deploy check — the evidence is untrusted data.

    Args:
        deploy_class: The deploy class under verification.
        runner: The command runner seam (shells ``curl``/``git``).
        expected_sha: The SHA expected to be deployed, if known.
        health_url: The health endpoint to probe.

    Returns:
        The assembled :class:`PrimaryArtifact`.
    """
    health_evidence = _run_evidence(runner, ["curl", "-fsS", "--max-time", "10", health_url])
    sha_evidence = _run_evidence(runner, ["git", "rev-parse", "HEAD"])
    untrusted = (
        f"DEPLOY CLASS: {deploy_class}\n\n"
        f"HEALTH ENDPOINT ({health_url}) EVIDENCE:\n{health_evidence}\n\n"
        f"DEPLOYED HEAD SHA EVIDENCE:\n{sha_evidence}\n"
    )
    reference = (
        "Pass criteria: the health endpoint must return a normal, healthy response — no error, "
        "timeout, connection-refused, 5xx, or degraded-status body. "
        + (
            f"The deployed SHA must exactly match the expected SHA: {expected_sha}."
            if expected_sha
            else "No expected SHA was specified for this check — verify health only."
        )
    )
    return PrimaryArtifact(
        kind="deploy_outcome",
        source=f"curl {health_url}; git rev-parse HEAD",
        trusted_reference=reference,
        untrusted=untrusted,
    )


def build_invocation(
    deploy_class: str,
    runner: CommandRunner,
    *,
    template_path: Path = DEPLOY_VERIFIER_TEMPLATE_PATH,
    repo_root: Path = Path("."),
    expected_sha: str | None = None,
    health_url: str = _DEFAULT_HEALTH_URL,
) -> SpecialistInvocation:
    """Assemble the deploy-verifier invocation from the fixed template + raw evidence.

    Args:
        deploy_class: The deploy class under verification.
        runner: The command runner seam.
        template_path: The fixed template.
        repo_root: Repository root for template resolution.
        expected_sha: The SHA expected to be deployed, if known.
        health_url: The health endpoint to probe.

    Returns:
        The assembled :class:`SpecialistInvocation`.
    """
    template = load_template(repo_root / template_path)
    artifact = fetch_deploy_artifact(
        deploy_class, runner, expected_sha=expected_sha, health_url=health_url
    )
    return assemble_invocation(template, artifact)


def verify_deploy(
    deploy_class: str,
    *,
    runner: CommandRunner,
    specialist_runner: SpecialistRunner,
    template_path: Path = DEPLOY_VERIFIER_TEMPLATE_PATH,
    repo_root: Path = Path("."),
    expected_sha: str | None = None,
    health_url: str = _DEFAULT_HEALTH_URL,
) -> Verdict:
    """Verify a post-deploy outcome and return the gating verdict.

    Note the only content input is ``deploy_class`` (+ the optional ``expected_sha``); ``runner`` and
    ``specialist_runner`` are IO seams and there is no framing/summary parameter, so master cannot
    prose the verifier (structural, mirroring ``review_pr``/``check_ticket_against_adrs``).

    Args:
        deploy_class: The deploy class under verification.
        runner: The command runner seam.
        specialist_runner: The specialist runner seam (fake in tests; ``claude -p`` in production).
        template_path: The fixed template.
        repo_root: Repository root.
        expected_sha: The SHA expected to be deployed, if known.
        health_url: The health endpoint to probe.

    Returns:
        The gating :class:`Verdict`.
    """
    inv = build_invocation(
        deploy_class,
        runner,
        template_path=template_path,
        repo_root=repo_root,
        expected_sha=expected_sha,
        health_url=health_url,
    )
    return run_specialist(inv, specialist_runner)


# --- CLI ---------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Checks authorization, or dry-runs/executes the post-deploy verifier."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--deploy-class", required=True, help="The deploy class (e.g. gateway_rebuild)."
    )
    parser.add_argument(
        "--check-authorization",
        action="store_true",
        help="Run only the mechanical AC-7 authorization gate (no LLM, no evidence fetch).",
    )
    parser.add_argument("--verify", action="store_true", help="Run the post-deploy evidence check.")
    parser.add_argument("--expected-sha", default=None, help="The SHA expected to be deployed.")
    parser.add_argument(
        "--health-url", default=_DEFAULT_HEALTH_URL, help="Health endpoint to probe."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the verifier live via claude -p (default: dry-run, no LLM call).",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output.")
    args = parser.parse_args(argv)

    logger = structlog.get_logger(__name__)
    trace_id = str(uuid.uuid4())

    if args.check_authorization:
        deploy = ProposedDeploy(deploy_class=args.deploy_class)
        authorized = deploy_authorized(deploy)
        logger.info(
            "deploy_authorization_check",
            trace_id=trace_id,
            deploy_class=args.deploy_class,
            authorized=authorized,
        )
        print(f"AUTHORIZED: {authorized}")
        return 0 if authorized else 2

    if not args.verify:
        parser.error("one of --check-authorization or --verify is required")

    inv = build_invocation(
        args.deploy_class,
        subprocess_runner,
        expected_sha=args.expected_sha,
        health_url=args.health_url,
    )
    logger.info(
        "specialist_review",
        trace_id=trace_id,
        specialist="deploy-verifier",
        deploy_class=args.deploy_class,
        template_version=inv.template.version,
        mode="execute" if args.execute else "dry-run",
    )

    if not args.execute:
        print(inv.prompt)
        return 0

    verdict = run_specialist(inv, claude_headless_runner())
    logger.info(
        "specialist_verdict",
        trace_id=trace_id,
        deploy_class=args.deploy_class,
        decision=verdict.decision,
        blocks_merge=blocks_merge(verdict),
        findings=len(verdict.findings),
    )
    if args.json:
        print(
            json.dumps(
                {
                    "decision": verdict.decision,
                    "findings": [
                        {"severity": f.severity, "category": f.category, "summary": f.summary}
                        for f in verdict.findings
                    ],
                },
                indent=2,
            )
        )
    else:
        print(f"VERDICT: {verdict.decision}")
        for finding in verdict.findings:
            print(f"  [{finding.severity}] {finding.category}: {finding.summary}")
    return 0 if verdict.decision == "APPROVE" else 2


if __name__ == "__main__":
    sys.exit(main())
