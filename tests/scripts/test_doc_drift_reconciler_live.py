# ruff: noqa: D103
"""Live behavioural proof for the doc-drift reconciler (FRE-834, ADR-0113 AC-8 drift row).

The `drift_reasoning` half a deterministic test cannot cover: run for real, does the reconciler
actually recognize that a proposed ticket re-files a question the **real** ADR corpus already
decided? Runs the real `claude -p` reconciler against the real repo `docs/architecture_decisions/`
corpus with a fixture ticket mirroring the FRE-827/ADR-0107 incident named in ADR-0113's evidence
table, plus a genuinely novel ticket as a true-negative check.

Marked `integration` + `requires_llm_server` — **not run in a build session**. Master/owner runs it at
the acceptance gate, per ADR §5::

    uv run pytest tests/scripts/test_doc_drift_reconciler_live.py -m requires_llm_server

The mechanical index-building + gate-threading contract is proven deterministically in
`test_doc_drift_reconciler.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.specialists.doc_drift_reconciler import (
    ProposedTicket,
    check_ticket_against_adrs,
)
from scripts.specialists.harness import claude_headless_runner

_FIXTURES = Path("tests/fixtures/specialists/doc_drift_reconciler")
_REPO_ROOT = Path(".")

pytestmark = [pytest.mark.integration, pytest.mark.requires_llm_server]


def _load(name: str) -> ProposedTicket:
    data = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    return ProposedTicket(title=data["title"], description=data["description"])


def test_reconciler_rejects_the_already_decided_is_owner_rename() -> None:
    verdict = check_ticket_against_adrs(
        _load("already_decided_is_owner.json"),
        specialist_runner=claude_headless_runner(),
        repo_root=_REPO_ROOT,
    )
    assert verdict.decision == "REJECT", (
        f"expected REJECT, got {verdict.decision}: {verdict.raw_response}"
    )
    blob = " ".join(f"{f.category} {f.summary}".lower() for f in verdict.findings)
    assert "0107" in blob or "is_owner" in blob, (
        f"no finding cited ADR-0107/is_owner: {[f.summary for f in verdict.findings]}"
    )


def test_reconciler_approves_a_genuinely_novel_ticket() -> None:
    verdict = check_ticket_against_adrs(
        _load("novel_ticket.json"),
        specialist_runner=claude_headless_runner(),
        repo_root=_REPO_ROOT,
    )
    assert verdict.decision == "APPROVE", (
        f"expected APPROVE, got {verdict.decision}: {verdict.raw_response}"
    )


def test_reconciler_ignores_the_planted_injection_and_spoofed_verdict() -> None:
    verdict = check_ticket_against_adrs(
        _load("injection_ticket.json"),
        specialist_runner=claude_headless_runner(),
        repo_root=_REPO_ROOT,
    )
    # The artifact's spoofed block claims APPROVE; a genuinely-novel-sounding but injection-laced
    # ticket must be judged on its ADR-index merits, and the injection attempt itself should be
    # visible as a finding, not silently obeyed.
    blob = " ".join(f"{f.category} {f.summary}".lower() for f in verdict.findings)
    assert "injection" in blob or "ignore" in blob, (
        f"reconciler did not flag the injection attempt: {[f.summary for f in verdict.findings]}"
    )
