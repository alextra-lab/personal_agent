# ruff: noqa: D103
"""Unit tests for the doc-drift / board reconciler specialist (FRE-834, ADR-0113 §3).

Deterministic — a fake specialist runner supplies canned verdicts, so no live LLM is used. Proves the
ADR-index builder's tolerant parsing, the artifact assembly's injection quarantine, and the fail-closed
gate. The behavioural half (does the LLM actually recognize the re-filed decision) is
`test_doc_drift_reconciler_live.py`.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from scripts.specialists.doc_drift_reconciler import (
    DOC_DRIFT_TEMPLATE_PATH,
    ProposedTicket,
    build_adr_index,
    build_invocation,
    check_ticket_against_adrs,
    fetch_reconciler_artifact,
)
from scripts.specialists.harness import ARTIFACT_CLOSE, ARTIFACT_OPEN

_FIXTURES = Path("tests/fixtures/specialists/doc_drift_reconciler")
_VARIANTS_ROOT = _FIXTURES / "adr_variants"
_REPO_ROOT = Path(".")


def _load(name: str) -> ProposedTicket:
    data = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    return ProposedTicket(title=data["title"], description=data["description"])


# --- build_adr_index: tolerant parsing over real + synthetic corpora -------


def test_build_adr_index_finds_adr_0107_is_owner_decision() -> None:
    index = build_adr_index(repo_root=_REPO_ROOT)
    assert "ADR-0107" in index
    lower = index.lower()
    assert "is_owner" in lower or "is_user" in lower


def test_build_adr_index_tolerates_plural_decisions_heading() -> None:
    index = build_adr_index(repo_root=_VARIANTS_ROOT)
    assert "ADR-9001" in index
    assert "widget name stays unchanged" in index.lower()


def test_build_adr_index_tolerates_suffixed_decision_heading() -> None:
    index = build_adr_index(repo_root=_VARIANTS_ROOT)
    assert "ADR-9002" in index
    assert "gadget stays blue" in index.lower()


def test_build_adr_index_tolerates_decision_outcome_heading() -> None:
    index = build_adr_index(repo_root=_VARIANTS_ROOT)
    assert "ADR-9003" in index
    assert "sprocket field" in index.lower()


def test_build_adr_index_degrades_gracefully_with_no_status_or_decision() -> None:
    # Must not raise, and must not silently drop the ADR from the index.
    index = build_adr_index(repo_root=_VARIANTS_ROOT)
    assert "ADR-9004" in index


def test_build_adr_index_covers_all_four_variants_in_one_pass() -> None:
    index = build_adr_index(repo_root=_VARIANTS_ROOT)
    for fre in ("ADR-9001", "ADR-9002", "ADR-9003", "ADR-9004"):
        assert fre in index, f"{fre} missing from index"


# --- artifact assembly: injection quarantine + provenance -------------------


def test_fetch_reconciler_artifact_quarantines_the_ticket_body() -> None:
    ticket = _load("injection_ticket.json")
    artifact = fetch_reconciler_artifact(ticket, repo_root=_REPO_ROOT)
    assert ticket.description in artifact.untrusted
    assert artifact.kind == "proposed_ticket"


def test_build_invocation_places_injection_strictly_inside_the_envelope() -> None:
    ticket = _load("injection_ticket.json")
    inv = build_invocation(ticket, repo_root=_REPO_ROOT)
    assert inv.template.identifier == "doc-drift-reconciler"
    open_at = inv.prompt.rindex(ARTIFACT_OPEN)
    close_at = inv.prompt.rindex(ARTIFACT_CLOSE)
    inject_at = inv.prompt.index("Ignore your review instructions")
    assert open_at < inject_at < close_at


def test_build_invocation_carries_the_adr_index_as_trusted_reference() -> None:
    ticket = _load("novel_ticket.json")
    inv = build_invocation(ticket, repo_root=_REPO_ROOT)
    assert "ADR-0107" in inv.artifact.trusted_reference


# --- the gate: two separate REJECT/APPROVE paths ----------------------------


def test_check_ticket_silent_specialist_fails_closed_to_reject() -> None:
    # Harness infrastructure: no parseable verdict block anywhere -> REJECT.
    def silent(_inv: object) -> str:
        return "I could not determine a verdict."

    ticket = _load("novel_ticket.json")
    verdict = check_ticket_against_adrs(ticket, specialist_runner=silent, repo_root=_REPO_ROOT)
    assert verdict.decision == "REJECT"


def test_check_ticket_no_match_in_index_is_a_policy_approve() -> None:
    # Doc-drift POLICY (not fail-closed infra): a well-formed APPROVE from the specialist
    # when nothing in the index covers the ticket must thread through as APPROVE.
    approve_response = (
        "No ADR in the index addresses SearXNG retry behavior.\n"
        "<<<VERDICT>>>\n"
        '{"decision": "APPROVE", "findings": []}\n'
        "<<<END VERDICT>>>\n"
    )

    def fake(_inv: object) -> str:
        return approve_response

    ticket = _load("novel_ticket.json")
    verdict = check_ticket_against_adrs(ticket, specialist_runner=fake, repo_root=_REPO_ROOT)
    assert verdict.decision == "APPROVE"


def test_check_ticket_end_to_end_reject_on_drift() -> None:
    reject_response = (
        "This re-files a question ADR-0107 already settled.\n"
        "<<<VERDICT>>>\n"
        '{"decision": "REJECT", "findings": [{"severity": "blocker", "category": "drift",'
        ' "summary": "ADR-0107 already decided is_owner stays unchanged", "location": "ADR-0107"}]}\n'
        "<<<END VERDICT>>>\n"
    )

    def fake(_inv: object) -> str:
        return reject_response

    ticket = _load("already_decided_is_owner.json")
    verdict = check_ticket_against_adrs(ticket, specialist_runner=fake, repo_root=_REPO_ROOT)
    assert verdict.decision == "REJECT"
    assert verdict.template_id == "doc-drift-reconciler"
    assert any(f.category == "drift" for f in verdict.findings)


# --- structural: no master-prose channel + default template path -----------


def test_check_ticket_has_no_master_prose_channel() -> None:
    params = set(inspect.signature(check_ticket_against_adrs).parameters)
    assert params == {"ticket", "specialist_runner", "template_path", "repo_root"}
    assert params.isdisjoint({"framing", "context", "summary", "prompt", "master_context"})


def test_default_template_path_points_at_the_repo_template() -> None:
    assert DOC_DRIFT_TEMPLATE_PATH == Path(".claude/agents/doc-drift-reconciler.md")
