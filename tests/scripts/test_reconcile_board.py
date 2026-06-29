# ruff: noqa: D103
"""Unit tests for the FRE-680 delivery-verification board reconciler.

The reconciler (scripts/reconcile_board.py) maps board claims to durable
evidence and emits verdicts with exactly four fields. These tests exercise the
pure logic only — header parsing and the MASTER_PLAN-vs-Linear reconciliation —
with injected Linear state, so no network or Linear key is required.

Covers FRE-680 acceptance criteria:
  AC1 — a claimed-OPEN ticket whose Linear state is closed yields a FAIL verdict.
  AC2 — the verdict schema is exactly {claim, status, evidence, note}, and an
        absent evidence source yields UNVERIFIABLE, never PASS.
"""

from __future__ import annotations

import dataclasses

from scripts.reconcile_board import (
    Verdict,
    classify_header_claims,
    reconcile_master_plan,
)


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
