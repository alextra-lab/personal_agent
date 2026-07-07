#!/usr/bin/env python3
"""PR-gate reviewer specialist (ADR-0113 §3, FRE-830).

The first judgment specialist: it reviews a worker PR for **correctness**,
**security**, and **acceptance-criteria against the backing ADR**, and returns a
:class:`~scripts.specialists.harness.Verdict` that gates autonomous merge. It runs
under the independence-protocol harness, so it reviews the **raw** diff (fetched
here via ``gh``, never a master summary), from the fixed
``.claude/agents/pr-gate-reviewer.md`` template, with the PR/commit/ticket text
quarantined as untrusted data.

**Advisory-only in FRE-830.** This module produces the gating *decision*; it wires
no real ``gh pr merge``. Autonomous merge, the sensitive-path carve-out, staged
graduation, and the durable owner-clearance source are FRE-835. Per ADR §5 this is
Phase A (shadow/advisory).

Callable by hand::

    python -m scripts.specialists.pr_gate --pr 419              # dry-run: print the assembled invocation
    python -m scripts.specialists.pr_gate --pr 419 --execute    # run the reviewer live (claude -p) + print the verdict
    python -m scripts.specialists.pr_gate --pr 419 --adr ADR-0113 --json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import uuid
from collections.abc import Sequence
from pathlib import Path

import structlog

from scripts.dispatch.launcher import CommandRunner, subprocess_runner
from scripts.specialists.harness import (
    PrimaryArtifact,
    SpecialistInvocation,
    SpecialistRunner,
    Verdict,
    assemble_invocation,
    blocks_merge,
    load_template,
    run_specialist,
)

# The fixed adversarial template this specialist runs from (harness-validated to
# live under .claude/agents/, content-versioned into every verdict).
PR_GATE_TEMPLATE_PATH = Path(".claude/agents/pr-gate-reviewer.md")

# The directory backing ADRs are read from, and the token grammar. Only a
# well-formed ``ADR-NNNN`` token is honoured, and only files under this directory
# are read — a PR-body-supplied token cannot traverse to an arbitrary path.
_ADR_DIR = Path("docs/architecture_decisions")
_ADR_TOKEN_RE = re.compile(r"ADR-(\d{4})")
_ADR_TOKEN_FULL_RE = re.compile(r"\AADR-(\d{4})\Z")

# ANSI/terminal control escape sequences, stripped from the dry-run print so a
# dry-run of an untrusted artifact can never be a terminal-injection vector.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# The default model for the live specialist run (Opus per ADR §6 — depth is
# bought with Opus subagents, not by downgrading).
DEFAULT_SPECIALIST_MODEL = "opus"
DEFAULT_SPECIALIST_TIMEOUT_S = 300.0


def _run_text(runner: CommandRunner, argv: Sequence[str]) -> str:
    """Run ``argv`` via the seam and return stdout, or ``""`` on non-zero exit."""
    result = runner(argv)
    return result.stdout if result.returncode == 0 else ""


def _first_adr_token(text: str) -> str | None:
    """Return the first ``ADR-NNNN`` token in ``text``, or ``None``."""
    match = _ADR_TOKEN_RE.search(text)
    return f"ADR-{match.group(1)}" if match else None


def _load_adr_reference(token: str | None, repo_root: Path) -> str:
    """Load a backing ADR's text as trusted reference, or a no-ADR note.

    Args:
        token: An ``ADR-NNNN`` token (from ``--adr`` or the PR body), or ``None``.
        repo_root: The repository root to resolve the ADR directory against.

    Returns:
        The ADR file text prefixed with its identifier, or a note that no backing
        ADR was found (in which case the reviewer checks correctness + security
        only and treats the acceptance-criteria dimension as N/A).
    """
    if token is None or not _ADR_TOKEN_FULL_RE.match(token):
        return (
            "No backing ADR resolved — review correctness + security only; acceptance-criteria N/A."
        )
    matches = sorted((repo_root / _ADR_DIR).glob(f"{token}-*.md"))
    if not matches:
        return (
            f"Backing ADR {token} referenced but its file was not found; acceptance-criteria N/A."
        )
    return f"# Backing ADR: {token}\n\n{matches[0].read_text(encoding='utf-8')}"


def _commit_text(commits: object) -> str:
    """Flatten ``gh pr view`` commit entries into one untrusted-text block."""
    if not isinstance(commits, list):
        return ""
    lines: list[str] = []
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        headline = str(commit.get("messageHeadline") or "")
        body = str(commit.get("messageBody") or "")
        lines.append(f"- {headline}\n{body}".rstrip())
    return "\n".join(lines)


def fetch_pr_artifact(
    pr: int,
    runner: CommandRunner,
    *,
    adr_override: str | None = None,
    repo_root: Path = Path("."),
) -> PrimaryArtifact:
    """Build the raw primary artifact for a PR — the raw diff, never a summary.

    The raw ``gh pr diff`` output and all PR/commit text are placed in the
    artifact's ``untrusted`` field (quarantined by the harness); the backing ADR
    (repo-checked) becomes the trusted reference. ``source`` records the fetch
    command as provenance proving the review saw the raw artifact.

    Args:
        pr: The PR number.
        runner: The command runner seam (shells ``gh``).
        adr_override: An explicit ``ADR-NNNN`` token; else auto-detected in the
            PR body.
        repo_root: Repository root for ADR resolution.

    Returns:
        The assembled :class:`PrimaryArtifact`.
    """
    diff = _run_text(runner, ["gh", "pr", "diff", str(pr)])
    view_raw = _run_text(
        runner, ["gh", "pr", "view", str(pr), "--json", "title,body,headRefName,commits"]
    )
    try:
        view = json.loads(view_raw or "{}")
    except json.JSONDecodeError:
        view = {}
    view_dict = view if isinstance(view, dict) else {}
    title = str(view_dict.get("title") or "")
    body = str(view_dict.get("body") or "")
    branch = str(view_dict.get("headRefName") or "")
    untrusted = (
        f"PR #{pr} — branch: {branch}\n"
        f"PR TITLE:\n{title}\n\n"
        f"PR BODY:\n{body}\n\n"
        f"COMMIT MESSAGES:\n{_commit_text(view_dict.get('commits'))}\n\n"
        f"RAW DIFF:\n{diff}"
    )
    reference = _load_adr_reference(adr_override or _first_adr_token(body), repo_root)
    return PrimaryArtifact(
        kind="pr_diff",
        source=f"gh pr diff {pr}",
        trusted_reference=reference,
        untrusted=untrusted,
    )


def build_invocation(
    pr: int,
    runner: CommandRunner,
    *,
    template_path: Path = PR_GATE_TEMPLATE_PATH,
    adr_override: str | None = None,
    repo_root: Path = Path("."),
) -> SpecialistInvocation:
    """Assemble the PR-gate reviewer invocation from the fixed template + raw PR.

    Args:
        pr: The PR number.
        runner: The command runner seam (shells ``gh``).
        template_path: The fixed template (default the PR-gate reviewer).
        adr_override: An explicit backing-ADR token, else auto-detected.
        repo_root: Repository root for template + ADR resolution.

    Returns:
        The assembled :class:`SpecialistInvocation`.
    """
    template = load_template(repo_root / template_path)
    artifact = fetch_pr_artifact(pr, runner, adr_override=adr_override, repo_root=repo_root)
    return assemble_invocation(template, artifact)


def review_pr(
    pr: int,
    *,
    runner: CommandRunner,
    specialist_runner: SpecialistRunner,
    template_path: Path = PR_GATE_TEMPLATE_PATH,
    adr_override: str | None = None,
    repo_root: Path = Path("."),
) -> Verdict:
    """Review a PR and return the gating verdict.

    Note the only content input is ``pr`` — ``runner`` and ``specialist_runner``
    are IO seams and there is no framing/summary/prompt parameter, so master
    cannot prose the reviewer through this entry point (structural AC-5).

    Args:
        pr: The PR number.
        runner: The command runner seam (shells ``gh``).
        specialist_runner: The specialist runner seam (fake in tests; ``claude
            -p`` in production).
        template_path: The fixed template.
        adr_override: An explicit backing-ADR token, else auto-detected.
        repo_root: Repository root.

    Returns:
        The gating :class:`Verdict`.
    """
    inv = build_invocation(
        pr, runner, template_path=template_path, adr_override=adr_override, repo_root=repo_root
    )
    return run_specialist(inv, specialist_runner)


def claude_headless_runner(
    model: str = DEFAULT_SPECIALIST_MODEL, *, timeout_s: float = DEFAULT_SPECIALIST_TIMEOUT_S
) -> SpecialistRunner:
    """Build the production specialist runner — a fresh, stateless ``claude -p``.

    No-tools is enforced at this boundary (``--allowed-tools`` with an empty
    allowlist), not trusted to the template frontmatter — the reviewer needs no
    tools because the artifact is already in its prompt.

    Args:
        model: The model to run (default Opus per ADR §6).
        timeout_s: Wall-clock timeout for the review.

    Returns:
        A :data:`SpecialistRunner` that runs the invocation's prompt via ``claude
        -p`` and returns its stdout.
    """

    def run(inv: SpecialistInvocation) -> str:
        result = subprocess.run(  # noqa: S603 - argv is fixed; prompt goes via stdin, no shell
            ["claude", "-p", "--model", model, "--allowed-tools", ""],  # noqa: S607
            input=inv.prompt,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return result.stdout

    return run


def _strip_ansi(text: str) -> str:
    """Remove ANSI/terminal control sequences (dry-run print hardening)."""
    return _ANSI_RE.sub("", text)


def _verdict_to_json(verdict: Verdict) -> dict[str, object]:
    """Serialize a verdict to a JSON-safe dict."""
    return {
        "decision": verdict.decision,
        "blocks_merge": blocks_merge(verdict),
        "template_id": verdict.template_id,
        "template_version": verdict.template_version,
        "artifact_source": verdict.artifact_source,
        "findings": [
            {
                "severity": f.severity,
                "category": f.category,
                "summary": f.summary,
                "location": f.location,
            }
            for f in verdict.findings
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Dry-runs (prints the assembled invocation) or runs the reviewer."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--pr", type=int, required=True, help="The PR number to review.")
    parser.add_argument("--adr", default=None, help="Backing ADR token (e.g. ADR-0113).")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the reviewer live via claude -p (default: dry-run, no LLM call).",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output.")
    args = parser.parse_args(argv)

    logger = structlog.get_logger(__name__)
    trace_id = str(uuid.uuid4())

    inv = build_invocation(args.pr, subprocess_runner, adr_override=args.adr)
    logger.info(
        "specialist_review",
        trace_id=trace_id,
        specialist="pr-gate-reviewer",
        pr=args.pr,
        template_version=inv.template.version,
        artifact_source=inv.artifact.source,
        mode="execute" if args.execute else "dry-run",
    )

    if not args.execute:
        if args.json:
            print(
                json.dumps(
                    {
                        "template_version": inv.template.version,
                        "artifact_source": inv.artifact.source,
                        "prompt": inv.prompt,
                    },
                    indent=2,
                )
            )
        else:
            print(_strip_ansi(inv.prompt))
        return 0

    verdict = run_specialist(inv, claude_headless_runner())
    logger.info(
        "specialist_verdict",
        trace_id=trace_id,
        pr=args.pr,
        decision=verdict.decision,
        blocks_merge=blocks_merge(verdict),
        findings=len(verdict.findings),
    )
    if args.json:
        print(json.dumps(_verdict_to_json(verdict), indent=2))
    else:
        print(f"VERDICT: {verdict.decision} (blocks_merge={blocks_merge(verdict)})")
        for finding in verdict.findings:
            print(
                f"  [{finding.severity}] {finding.category}: {finding.summary} ({finding.location})"
            )
    return 0 if verdict.decision == "APPROVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
