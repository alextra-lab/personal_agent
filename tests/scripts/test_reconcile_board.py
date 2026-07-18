# ruff: noqa: D103
"""Unit tests for the FRE-680/FRE-861/FRE-915 delivery-board reconciler.

The reconciler (scripts/reconcile_board.py) maps board *claims* to durable
*evidence* and emits verdicts with exactly four fields. It is a mechanical
helper signal for master's judgment, not a replacement for it: it only ever
compares Linear ticket state against repo facts (a merged PR exists or
doesn't) — never whether a specific acceptance criterion's content is
proven, which requires interpretation and stays master's job.

Covers FRE-680 acceptance criteria:
  AC1 — a claimed-OPEN ticket whose Linear state is closed yields a FAIL verdict.
  AC2 — the verdict schema is exactly {claim, status, evidence, note}, and an
        absent evidence source yields UNVERIFIABLE, never PASS.

Covers FRE-915 acceptance criteria (supersedes FRE-861's MASTER_PLAN-prose
parsing, retired because the forward-only MASTER_PLAN rewrite (PR 573)
carries no status narrative to parse):
  AC1 — the claim set comes from Linear ticket state, not MASTER_PLAN text.
        No test in this file reads or asserts on MASTER_PLAN's prose content.
  AC2 — (proven live, not by a unit test — see the PR's post-deploy runbook)
        running the reconciler against the current forward-only MASTER_PLAN
        succeeds, because the claim set no longer depends on it at all.
  AC3 — the fail-loud-on-zero behaviour is preserved, but scoped to the right
        layer: an empty *claim set* (nothing fetched from Linear at all — no
        key, or a query failure) is the forced-failure condition. A non-empty
        claim set that legitimately yields zero *verdicts* (a healthy day —
        nothing drifted) is NOT a forced failure; a code-review pass caught
        this exact conflation (the two were collapsed onto one "empty
        verdicts" check, so every healthy day exited 1).
  AC4 — a ticket whose Linear state disagrees with the repository (a merged
        PR already exists for a ticket Linear still shows as open) is
        detected end-to-end as a FAIL verdict, via an injected fake PR
        finder — no network or gh CLI involved.

Also covers a code-review-caught correctness fix: ticket-to-PR matching must
be anchored to a whole branch-name segment, not plain substring containment
— otherwise a shorter ticket id (FRE-9) falsely matches a longer one's branch
(FRE-900's `fre-900-...`).
"""

from __future__ import annotations

import dataclasses

from scripts.reconcile_board import (
    Verdict,
    _branch_matches_ticket,
    _make_pr_finder,
    exit_code_for,
    reconcile_claims_vs_repo,
    verdict_for_claim,
)


def test_verdict_has_exactly_four_fields() -> None:
    fields = {f.name for f in dataclasses.fields(Verdict)}
    assert fields == {"claim", "status", "evidence", "note"}


def test_verdict_status_domain_is_three_values() -> None:
    # The reconciler must only ever emit these three statuses.
    v = Verdict(claim="x", status="UNVERIFIABLE", evidence=[], note="")
    assert v.status in {"PASS", "FAIL", "UNVERIFIABLE"}


# --- verdict_for_claim: expected-PR states (Done / Awaiting Deploy / Verify Failed) ---


def test_done_with_merged_pr_is_pass() -> None:
    prs = [{"number": 42, "headRefName": "fre-900-x", "mergedAt": "2026-07-01T00:00:00Z"}]
    verdict = verdict_for_claim("FRE-900", "Done", prs)
    assert verdict is not None
    assert verdict.status == "PASS"
    assert "FRE-900" in verdict.evidence[0] or any("FRE-900" in e for e in verdict.evidence)


def test_done_with_no_merged_pr_is_unverifiable_never_pass() -> None:
    # AC2 (FRE-680): a missing source must be UNVERIFIABLE, never silently PASS.
    verdict = verdict_for_claim("FRE-900", "Done", [])
    assert verdict is not None
    assert verdict.status == "UNVERIFIABLE"


def test_done_with_gh_unavailable_is_unverifiable() -> None:
    verdict = verdict_for_claim("FRE-900", "Done", None)
    assert verdict is not None
    assert verdict.status == "UNVERIFIABLE"


def test_awaiting_deploy_with_merged_pr_is_pass() -> None:
    prs = [{"number": 1, "headRefName": "fre-901-y", "mergedAt": "2026-07-02T00:00:00Z"}]
    verdict = verdict_for_claim("FRE-901", "Awaiting Deploy", prs)
    assert verdict is not None
    assert verdict.status == "PASS"


def test_verify_failed_with_merged_pr_is_pass_not_drift() -> None:
    # A merged PR is normal and expected for Verify Failed (it merged, then
    # post-deploy verification failed) — must NOT be read as drift.
    prs = [{"number": 2, "headRefName": "fre-902-z", "mergedAt": "2026-07-03T00:00:00Z"}]
    verdict = verdict_for_claim("FRE-902", "Verify Failed", prs)
    assert verdict is not None
    assert verdict.status == "PASS"


# --- verdict_for_claim: AC4 — the drift case ---------------------------------


def test_in_progress_with_merged_pr_is_fail() -> None:
    # AC4: Linear still shows In Progress but a merged PR already exists for
    # this ticket's branch — the GitHub auto-transition was likely missed.
    prs = [{"number": 7, "headRefName": "fre-903-drift", "mergedAt": "2026-07-04T00:00:00Z"}]
    verdict = verdict_for_claim("FRE-903", "In Progress", prs)
    assert verdict is not None
    assert verdict.status == "FAIL"
    assert "FRE-903" in verdict.claim
    assert any("7" in e or "fre-903-drift" in e for e in verdict.evidence)


def test_approved_with_merged_pr_is_fail() -> None:
    prs = [{"number": 8, "headRefName": "fre-904-drift", "mergedAt": "2026-07-04T00:00:00Z"}]
    verdict = verdict_for_claim("FRE-904", "Approved", prs)
    assert verdict is not None
    assert verdict.status == "FAIL"


def test_in_review_with_merged_pr_is_fail() -> None:
    prs = [{"number": 9, "headRefName": "fre-905-drift", "mergedAt": "2026-07-04T00:00:00Z"}]
    verdict = verdict_for_claim("FRE-905", "In Review", prs)
    assert verdict is not None
    assert verdict.status == "FAIL"


# --- verdict_for_claim: nothing to check yet ---------------------------------


def test_in_progress_with_no_merged_pr_yields_no_verdict() -> None:
    # Ticket is genuinely still in flight — nothing to check yet.
    assert verdict_for_claim("FRE-906", "In Progress", []) is None


def test_in_progress_with_gh_unavailable_yields_no_verdict() -> None:
    assert verdict_for_claim("FRE-906", "In Progress", None) is None


def test_canceled_yields_no_verdict_regardless_of_prs() -> None:
    # Ambiguous: a canceled ticket may or may not have had a PR either way.
    assert verdict_for_claim("FRE-907", "Canceled", []) is None
    prs = [{"number": 3, "headRefName": "fre-907-x", "mergedAt": "2026-07-01T00:00:00Z"}]
    assert verdict_for_claim("FRE-907", "Canceled", prs) is None


def test_duplicate_yields_no_verdict() -> None:
    assert verdict_for_claim("FRE-908", "Duplicate", []) is None


# --- verdict_for_claim: state-name normalization ------------------------------


def test_state_matching_is_case_and_whitespace_insensitive() -> None:
    prs = [{"number": 1, "headRefName": "fre-909-x", "mergedAt": "2026-07-01T00:00:00Z"}]
    verdict = verdict_for_claim("FRE-909", "  DONE  ", prs)
    assert verdict is not None
    assert verdict.status == "PASS"


# --- reconcile_claims_vs_repo: composition + AC3 (fail loud on zero) --------


def test_reconcile_claims_vs_repo_composes_pr_finder_per_ticket() -> None:
    claims = {"FRE-900": "Done", "FRE-906": "In Progress"}

    def fake_finder(fre_id: str) -> list[dict[str, object]] | None:
        if fre_id == "FRE-900":
            return [{"number": 1, "headRefName": "fre-900-x", "mergedAt": "2026-07-01T00:00:00Z"}]
        return []

    verdicts = reconcile_claims_vs_repo(claims, pr_finder=fake_finder)
    # FRE-906 (In Progress, no PR) yields no verdict — only FRE-900 does.
    assert len(verdicts) == 1
    assert verdicts[0].status == "PASS"


def test_reconcile_claims_vs_repo_empty_claims_yields_empty_verdicts() -> None:
    # The upstream forced-failure condition (AC3) is an empty *claim set* —
    # tested at the `main()` level via the live PR runbook, since fetching is
    # impure. Here we only confirm the pure composition: no claims in, no
    # verdicts out.
    verdicts = reconcile_claims_vs_repo({}, pr_finder=lambda fre_id: [])
    assert verdicts == []


def test_healthy_day_nonempty_claims_zero_verdicts_is_not_a_forced_failure() -> None:
    # AC3, corrected: a *non-empty* claim set where every ticket legitimately
    # has nothing to check (still in flight, no PR yet) yields zero verdicts
    # — and that must NOT read as a failure. Conflating this with "the claim
    # fetch itself came back empty" was the exact bug a code-review pass
    # caught: every healthy day exited 1.
    claims = {"FRE-906": "In Progress", "FRE-920": "Approved"}
    verdicts = reconcile_claims_vs_repo(claims, pr_finder=lambda fre_id: [])
    assert verdicts == []
    assert exit_code_for(verdicts) == 0


def test_exit_code_for_empty_verdicts_is_zero_not_a_forced_failure() -> None:
    # exit_code_for's contract is now purely "did anything FAIL" — an empty
    # list has no FAILs. The "nothing was checked at all" signal lives one
    # layer up, on the fetched claim set (see main()), not here.
    assert exit_code_for([]) == 0


def test_exit_code_for_all_pass_is_zero() -> None:
    verdicts = [Verdict(claim="x", status="PASS", evidence=[], note="")]
    assert exit_code_for(verdicts) == 0


def test_exit_code_for_any_fail_is_nonzero() -> None:
    verdicts = [
        Verdict(claim="x", status="PASS", evidence=[], note=""),
        Verdict(claim="y", status="FAIL", evidence=[], note=""),
    ]
    assert exit_code_for(verdicts) == 1


# --- _branch_matches_ticket / _make_pr_finder: anchored matching -------------


def test_branch_matches_ticket_exact_prefix() -> None:
    assert _branch_matches_ticket("fre-900-fix-thing", "FRE-900")


def test_branch_matches_ticket_rejects_numeric_prefix_collision() -> None:
    # The found bug: plain substring containment made "fre-9" match inside
    # "fre-900-...", and "fre-90" match inside "fre-900-...".
    assert not _branch_matches_ticket("fre-900-fix-thing", "FRE-9")
    assert not _branch_matches_ticket("fre-900-fix-thing", "FRE-90")


def test_branch_matches_ticket_handles_path_prefixed_branch() -> None:
    # Real branch observed live: docs/fre-893-redo-done-2026-07-16.
    assert _branch_matches_ticket("docs/fre-893-redo-done-2026-07-16", "FRE-893")


def test_branch_matches_ticket_exact_no_slug() -> None:
    assert _branch_matches_ticket("fre-9", "FRE-9")


def test_make_pr_finder_excludes_numeric_prefix_collision() -> None:
    merged_prs = [
        {"number": 1, "headRefName": "fre-900-fix-thing", "mergedAt": "2026-07-01T00:00:00Z"}
    ]
    finder = _make_pr_finder(merged_prs)
    assert finder("FRE-9") == []
    assert len(finder("FRE-900")) == 1


def test_make_pr_finder_none_batch_propagates_none_per_ticket() -> None:
    finder = _make_pr_finder(None)
    assert finder("FRE-900") is None
