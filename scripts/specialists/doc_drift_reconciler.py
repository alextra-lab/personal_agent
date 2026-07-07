#!/usr/bin/env python3
"""Doc-drift / board reconciler specialist (ADR-0113 §3, FRE-834).

The third judgment specialist: catches **ADR-memory drift** — a proposed ticket or decision that
re-files a question an existing, accepted ADR already decided (the class named in ADR-0113's evidence
table: master filed FRE-827 as net-new when ADR-0107 had already decided it). It runs under the
independence-protocol harness, so it reviews the raw ticket text against a deterministic, repo-checked
digest of the ADR corpus (:func:`build_adr_index`), from the fixed
``.claude/agents/doc-drift-reconciler.md`` template, with the ticket text quarantined as untrusted data.

This module is separate from and does not modify ``scripts/reconcile_board.py`` (FRE-680): that script
stays LLM-free and covers the MASTER_PLAN ↔ Linear ↔ merged-PR evidence checks; this module is the
LLM-judgment half ADR-0113 names as the catcher — "cross-checks a new ticket or decision against
existing ADRs" — a semantic comparison no deterministic parser can make.

**Advisory-only in FRE-834.** This module produces the drift-detection *verdict*; it wires no real
ticket-creation gate. Per ADR §5 this is Phase A (shadow/advisory). No ``src/`` behavior — dev-process
tooling under ``scripts/``.

Callable by hand::

    python -m scripts.specialists.doc_drift_reconciler --ticket ticket.json           # dry-run
    python -m scripts.specialists.doc_drift_reconciler --ticket ticket.json --execute  # run live + print verdict
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

import structlog

from scripts.specialists.harness import (
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

# The fixed adversarial template this specialist runs from (harness-validated to live under
# .claude/agents/, content-versioned into every verdict).
DOC_DRIFT_TEMPLATE_PATH = Path(".claude/agents/doc-drift-reconciler.md")

# The directory the ADR corpus is read from — only ADR-numbered files, not meta-docs
# (README.md, RISK_AND_TRADEOFFS.md, etc.) that happen to also live here.
_ADR_DIR = Path("docs/architecture_decisions")
_ADR_GLOB = "ADR-*.md"

# The maximum length of a decision excerpt placed into the index, whitespace-collapsed. Keeps the
# corpus digest bounded (~120 files x ~500 chars is a manageable single reference block) rather than
# feeding every ADR's full text into every reconciliation call.
_EXCERPT_MAX_CHARS = 500
# The fallback excerpt length when no Decision-shaped heading is found at all.
_FALLBACK_EXCERPT_MAX_CHARS = 400

_TITLE_RE = re.compile(r"^#\s*(ADR-\d{4}.*)$", re.MULTILINE)
_NUMBER_RE = re.compile(r"ADR-(\d{4})")
_STATUS_RE = re.compile(r"(?im)^\**Status\**:?\s*(.+?)\s*$")
# Tolerant of the real corpus's heading variants: "## Decision", "## Decisions" (plural), a suffixed
# "## Decision — Final Ruling", and "## Decision Outcome" — matched on the leading word only, not an
# exact-string heading match.
_DECISION_HEADING_RE = re.compile(r"(?im)^(#{1,6})\s*Decisions?\b.*$")
_ANY_HEADING_RE = re.compile(r"(?m)^#{1,6}\s")


def _collapse_whitespace(text: str) -> str:
    """Collapse runs of whitespace to single spaces and strip the result."""
    return re.sub(r"\s+", " ", text).strip()


def _extract_title(text: str, fallback: str) -> str:
    """Return the ``# ADR-NNNN: Title`` line's content, or ``fallback`` if absent."""
    match = _TITLE_RE.search(text)
    return _collapse_whitespace(match.group(1)) if match else fallback


def _extract_status(text: str) -> str:
    """Return the ``**Status:**`` value, or ``"unknown"`` if the file carries none."""
    match = _STATUS_RE.search(text)
    return _collapse_whitespace(match.group(1)) if match else "unknown"


def _extract_decision_excerpt(text: str) -> str:
    """Return a bounded decision excerpt — the Decision-shaped section, or a body fallback.

    Tolerant of heading-format drift across the corpus's history (plural, suffixed, "Outcome"
    variants); an ADR with no Decision-shaped heading at all degrades to the leading body text
    rather than being dropped from the index or raising.
    """
    heading_match = _DECISION_HEADING_RE.search(text)
    if heading_match is not None:
        body_start = heading_match.end()
        next_heading = _ANY_HEADING_RE.search(text, pos=body_start)
        body_end = next_heading.start() if next_heading else len(text)
        excerpt = text[body_start:body_end]
        return _collapse_whitespace(excerpt)[:_EXCERPT_MAX_CHARS]
    # Fallback: no Decision-shaped heading anywhere — use the leading body text (skip the title line).
    lines = [line for line in text.splitlines() if not line.strip().startswith("#")]
    return _collapse_whitespace(" ".join(lines))[:_FALLBACK_EXCERPT_MAX_CHARS]


def build_adr_index(repo_root: Path = Path(".")) -> str:
    """Build a deterministic, tolerant digest of every ADR in the corpus.

    One line per ADR: ``ADR-NNNN (<status>): <title> — <decision excerpt>``, sorted by ADR number.
    Best-effort parsing — an ADR with an unrecognized Status/Decision heading format degrades to
    ``status unknown`` / a leading-body-text excerpt rather than raising or being silently dropped, so
    older corpus entries still contribute to the reconciliation instead of creating a blind spot.

    Args:
        repo_root: Repository root the ADR directory is resolved against.

    Returns:
        The formatted, newline-joined index text (trusted reference material for the specialist).
    """
    adr_dir = repo_root / _ADR_DIR
    entries: list[tuple[int, str]] = []
    for path in sorted(adr_dir.glob(_ADR_GLOB)):
        match = _NUMBER_RE.search(path.name)
        if match is None:
            continue
        number = int(match.group(1))
        text = path.read_text(encoding="utf-8")
        title = _extract_title(text, fallback=path.stem)
        status = _extract_status(text)
        excerpt = _extract_decision_excerpt(text)
        entries.append((number, f"ADR-{number:04d} ({status}): {title} — {excerpt}"))
    entries.sort(key=lambda pair: pair[0])
    return "\n".join(line for _, line in entries)


# --- the reconciler specialist (runs under the harness) ---------------------


@dataclasses.dataclass(frozen=True)
class ProposedTicket:
    """A proposed ticket or decision under doc-drift scrutiny.

    Attributes:
        title: The ticket's title.
        description: The ticket's body — untrusted data reviewed by the specialist.
    """

    title: str
    description: str


def fetch_reconciler_artifact(
    ticket: ProposedTicket, *, repo_root: Path = Path(".")
) -> PrimaryArtifact:
    """Build the raw primary artifact for a proposed ticket — the ticket is untrusted data.

    Args:
        ticket: The proposed ticket under scrutiny.
        repo_root: Repository root for ADR-index resolution.

    Returns:
        The assembled :class:`PrimaryArtifact`: the ticket title + description as untrusted data, the
        ADR index as trusted reference.
    """
    untrusted = f"PROPOSED TICKET\nTITLE: {ticket.title}\n\nDESCRIPTION:\n{ticket.description}"
    return PrimaryArtifact(
        kind="proposed_ticket",
        source="proposed-ticket",
        trusted_reference=build_adr_index(repo_root=repo_root),
        untrusted=untrusted,
    )


def build_invocation(
    ticket: ProposedTicket,
    *,
    template_path: Path = DOC_DRIFT_TEMPLATE_PATH,
    repo_root: Path = Path("."),
) -> SpecialistInvocation:
    """Assemble the reconciler invocation from the fixed template + the proposed ticket.

    Args:
        ticket: The proposed ticket under scrutiny.
        template_path: The fixed reconciler template.
        repo_root: Repository root for template + ADR-index resolution.

    Returns:
        The assembled :class:`SpecialistInvocation`.
    """
    template = load_template(repo_root / template_path)
    artifact = fetch_reconciler_artifact(ticket, repo_root=repo_root)
    return assemble_invocation(template, artifact)


def check_ticket_against_adrs(
    ticket: ProposedTicket,
    *,
    specialist_runner: SpecialistRunner,
    template_path: Path = DOC_DRIFT_TEMPLATE_PATH,
    repo_root: Path = Path("."),
) -> Verdict:
    """Run the reconciler over a proposed ticket and return its verdict.

    The only content input is ``ticket``; ``specialist_runner`` is an IO seam and there is no
    framing/summary parameter, so master cannot prose the reconciler.

    Args:
        ticket: The proposed ticket under scrutiny.
        specialist_runner: The specialist runner seam (fake in tests; ``claude -p`` in production).
        template_path: The fixed reconciler template.
        repo_root: Repository root.

    Returns:
        The reconciler :class:`Verdict`.
    """
    inv = build_invocation(ticket, template_path=template_path, repo_root=repo_root)
    return run_specialist(inv, specialist_runner)


# --- CLI ---------------------------------------------------------------------


def _load_ticket(path: Path) -> ProposedTicket:
    """Load a :class:`ProposedTicket` from a JSON file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"ticket file {path} must be a JSON object")
    return ProposedTicket(
        title=str(data.get("title") or ""), description=str(data.get("description") or "")
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Dry-runs (prints the assembled invocation) or runs the reconciler."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ticket", required=True, help="Path to a JSON ProposedTicket file.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the reconciler live via claude -p (default: dry-run, no LLM call).",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output.")
    args = parser.parse_args(argv)

    logger = structlog.get_logger(__name__)
    trace_id = str(uuid.uuid4())

    ticket = _load_ticket(Path(args.ticket))
    inv = build_invocation(ticket)
    logger.info(
        "specialist_review",
        trace_id=trace_id,
        specialist="doc-drift-reconciler",
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
