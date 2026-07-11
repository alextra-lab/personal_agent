# ruff: noqa: D103
"""Unit tests for the FRE-680/FRE-861 delivery-verification board reconciler.

The reconciler (scripts/reconcile_board.py) maps board claims to durable
evidence and emits verdicts with exactly four fields. These tests exercise the
pure logic only — header/body parsing, the MASTER_PLAN-vs-Linear
reconciliation, and the Implemented/live-vs-live-evidence check — with
injected Linear state and probes, so no network or Linear key is required.

Covers FRE-680 acceptance criteria:
  AC1 — a claimed-OPEN ticket whose Linear state is closed yields a FAIL verdict.
  AC2 — the verdict schema is exactly {claim, status, evidence, note}, and an
        absent evidence source yields UNVERIFIABLE, never PASS.

Covers FRE-861 acceptance criteria:
  AC1 — running against the current MASTER_PLAN extracts a non-zero claim set
        (widened body-section scope, not just the header).
  AC2 — an all-empty parse exits non-zero (fail loud on zero verdicts).
  AC3 — a plan claim of Implemented with no backing live evidence is reported
        as a FAIL.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from scripts.reconcile_board import (
    ImplementedClaim,
    Verdict,
    check_live_evidence,
    classify_header_claims,
    exit_code_for,
    extract_body_sections,
    extract_claims_text,
    extract_implemented_claims,
    reconcile_master_plan,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MASTER_PLAN_PATH = _REPO_ROOT / "docs" / "plans" / "MASTER_PLAN.md"


def test_verdict_has_exactly_four_fields() -> None:
    fields = {f.name for f in dataclasses.fields(Verdict)}
    assert fields == {"claim", "status", "evidence", "note"}


def test_verdict_status_domain_is_three_values() -> None:
    # The reconciler must only ever emit these three statuses.
    v = Verdict(claim="x", status="UNVERIFIABLE", evidence=[], note="")
    assert v.status in {"PASS", "FAIL", "UNVERIFIABLE"}


def test_header_stays_in_progress_classifies_open() -> None:
    header = "Recall work continues (FRE-655 stays In Progress): owner live test pending."
    claims = classify_header_claims(header)
    assert claims.get("FRE-655") == "OPEN"


def test_adversarial_was_in_progress_then_shipped_classifies_done() -> None:
    # Past-tense "was In Progress" must NOT be read as a current OPEN claim;
    # the present "shipped" wins → DONE. Guards AC1 against a false positive.
    header = "FRE-655 was In Progress yesterday. FRE-655 shipped and is Done."
    claims = classify_header_claims(header)
    assert claims.get("FRE-655") == "DONE"


def test_done_word_classifies_done() -> None:
    header = "FRE-672 SHIPPED + Done (PR #274); FRE-673 DONE (PR #276)."
    claims = classify_header_claims(header)
    assert claims.get("FRE-672") == "DONE"
    assert claims.get("FRE-673") == "DONE"


def test_check_a_open_vs_linear_done_is_fail() -> None:
    # AC1: header narrates FRE-655 In Progress while Linear shows it closed.
    claims = {"FRE-655": "OPEN"}
    linear = {"FRE-655": "Done"}
    verdicts = reconcile_master_plan(claims, linear)
    fail = [v for v in verdicts if v.status == "FAIL" and "FRE-655" in v.claim]
    assert len(fail) == 1
    # Evidence cites both sources.
    assert any("FRE-655" in cite for cite in fail[0].evidence)


def test_check_a_matching_state_is_pass() -> None:
    claims = {"FRE-655": "OPEN"}
    linear = {"FRE-655": "In Progress"}
    verdicts = reconcile_master_plan(claims, linear)
    assert [v.status for v in verdicts] == ["PASS"]


def test_check_a_unreachable_linear_is_unverifiable_never_pass() -> None:
    # AC2: a missing source must be UNVERIFIABLE, never silently PASS.
    claims = {"FRE-655": "OPEN"}
    linear: dict[str, str | None] = {"FRE-655": None}
    verdicts = reconcile_master_plan(claims, linear)
    assert [v.status for v in verdicts] == ["UNVERIFIABLE"]
    assert all(v.status != "PASS" for v in verdicts)


def test_check_a_claimed_done_but_linear_open_is_fail() -> None:
    claims = {"FRE-999": "DONE"}
    linear = {"FRE-999": "In Progress"}
    verdicts = reconcile_master_plan(claims, linear)
    assert [v.status for v in verdicts] == ["FAIL"]


# --- FRE-861 AC1: widened scope — body sections, not just the header --------


def test_extract_body_sections_collects_named_section_only() -> None:
    text = (
        "# Master Plan\n"
        "\n"
        "## Current live-env & standing state\n"
        "\n"
        "- FRE-900 shipped and is Done.\n"
        "\n"
        "## Some other section\n"
        "\n"
        "- FRE-901 shipped and is Done.\n"
    )
    body = extract_body_sections(text, headings=("## Current live-env & standing state",))
    assert "FRE-900" in body
    assert "FRE-901" not in body


def test_extract_body_sections_stops_at_next_heading() -> None:
    text = (
        "## Current live-env & standing state\n"
        "- FRE-900 shipped and is Done.\n"
        "## Open threads (current)\n"
        "- FRE-901 stays In Progress.\n"
    )
    body = extract_body_sections(text, headings=("## Current live-env & standing state",))
    assert "FRE-900" in body
    assert "FRE-901" not in body


def test_body_section_ticket_claims_are_classified() -> None:
    # A ticket claim that now lives only in the body (not the header) must
    # still be extracted — this is the exact drift the header-only parser missed.
    text = (
        "> **Last updated**: 2026-07-11\n"
        "\n"
        "## Current live-env & standing state\n"
        "\n"
        "- FRE-900 shipped and is Done.\n"
    )
    claims_text = extract_claims_text(text)
    claims = classify_header_claims(claims_text)
    assert claims.get("FRE-900") == "DONE"


def test_real_master_plan_extracts_nonzero_claim_set() -> None:
    # AC1 literal wording: running against the *current* MASTER_PLAN extracts
    # a non-zero claim set.
    text = _MASTER_PLAN_PATH.read_text()
    claims_text = extract_claims_text(text)
    ticket_claims = classify_header_claims(claims_text)
    implemented_claims = extract_implemented_claims(claims_text)
    assert len(ticket_claims) + len(implemented_claims) > 0


# --- FRE-861 AC2: fail loud on zero verdicts ---------------------------------


def test_exit_code_for_empty_verdicts_is_nonzero() -> None:
    assert exit_code_for([]) == 1


def test_exit_code_for_all_pass_is_zero() -> None:
    verdicts = [Verdict(claim="x", status="PASS", evidence=[], note="")]
    assert exit_code_for(verdicts) == 0


def test_exit_code_for_any_fail_is_nonzero() -> None:
    verdicts = [
        Verdict(claim="x", status="PASS", evidence=[], note=""),
        Verdict(claim="y", status="FAIL", evidence=[], note=""),
    ]
    assert exit_code_for(verdicts) == 1


def test_empty_master_plan_yields_zero_verdicts() -> None:
    # An all-empty parse (no header, no known body sections) must extract
    # nothing, which exit_code_for then treats as a forced failure.
    claims_text = extract_claims_text("# Master Plan\n\nNothing here.\n")
    ticket_claims = classify_header_claims(claims_text)
    implemented_claims = extract_implemented_claims(claims_text)
    assert ticket_claims == {}
    assert implemented_claims == []
    assert exit_code_for([]) == 1


# --- FRE-861 AC3: Implemented/live claims must map to live evidence ---------


def test_extract_implemented_claims_finds_adr_implemented() -> None:
    text = "Config: ADR-0099 config management **Implemented**. Linear keys rotated."
    claims = extract_implemented_claims(text)
    assert [c.subject for c in claims] == ["ADR-0099"]


def test_extract_implemented_claims_finds_adr_live() -> None:
    text = "ADR-0109 V2 10-type is genuinely live end-to-end."
    claims = extract_implemented_claims(text)
    assert [c.subject for c in claims] == ["ADR-0109"]


def test_extract_implemented_claims_excludes_negated_clause() -> None:
    text = "ADR-0104 Partial (2-arm live; structural arm unwired — NOT Implemented)."
    claims = extract_implemented_claims(text)
    assert "ADR-0104" not in [c.subject for c in claims]


def test_implemented_claim_with_no_probe_registered_is_fail() -> None:
    # AC3: a plan claim of Implemented with no backing live evidence -> FAIL.
    claim = ImplementedClaim(subject="ADR-0104", clause="ADR-0104 Implemented")
    verdict = check_live_evidence(claim, probes={})
    assert verdict.status == "FAIL"
    assert "ADR-0104" in verdict.note


def test_implemented_claim_with_confirming_probe_is_pass() -> None:
    claim = ImplementedClaim(subject="ADR-0104", clause="ADR-0104 Implemented")
    verdict = check_live_evidence(claim, probes={"ADR-0104": lambda: True})
    assert verdict.status == "PASS"


def test_implemented_claim_with_contradicting_probe_is_fail() -> None:
    claim = ImplementedClaim(subject="ADR-0104", clause="ADR-0104 Implemented")
    verdict = check_live_evidence(claim, probes={"ADR-0104": lambda: False})
    assert verdict.status == "FAIL"


def test_implemented_claim_with_unevaluable_probe_is_unverifiable() -> None:
    claim = ImplementedClaim(subject="ADR-0104", clause="ADR-0104 Implemented")
    verdict = check_live_evidence(claim, probes={"ADR-0104": lambda: None})
    assert verdict.status == "UNVERIFIABLE"


def test_implemented_claim_with_raising_probe_is_unverifiable_not_a_crash() -> None:
    def _boom() -> bool | None:
        raise RuntimeError("substrate unreachable")

    claim = ImplementedClaim(subject="ADR-0104", clause="ADR-0104 Implemented")
    verdict = check_live_evidence(claim, probes={"ADR-0104": _boom})
    assert verdict.status == "UNVERIFIABLE"
    assert "substrate unreachable" in verdict.note


def test_check_live_evidence_verdict_has_exactly_four_fields() -> None:
    claim = ImplementedClaim(subject="ADR-0104", clause="ADR-0104 Implemented")
    verdict = check_live_evidence(claim, probes={})
    assert {f.name for f in dataclasses.fields(verdict)} == {
        "claim",
        "status",
        "evidence",
        "note",
    }
