"""Tests for FRE-1011 guard against ticket tokens in PR title and docs branches."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add scripts to path for import
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from guard_pr_ticket_tokens import (
    check_branch_name,
    check_pr_body_warning,
    check_pr_title,
)


class TestCheckBranchName:
    """Tests for branch name validation."""

    def test_delivering_branch_with_token_passes(self) -> None:
        """Normal delivering branch with FRE token should pass."""
        is_valid, msg = check_branch_name("fre-1011-trace-id-cost-ledger")
        assert is_valid is True
        assert msg == ""

    def test_docs_branch_without_token_passes(self) -> None:
        """Docs branch without FRE token should pass."""
        is_valid, msg = check_branch_name("docs/adr-0125-accepted")
        assert is_valid is True
        assert msg == ""

    def test_docs_branch_with_token_fails(self) -> None:
        """Docs branch with FRE token should fail."""
        is_valid, msg = check_branch_name("docs/fre-1011-something")
        assert is_valid is False
        assert "FRE-1011" in msg or "FRE-1011" in msg.upper()
        assert "topic slug" in msg

    def test_docs_slash_prefix_with_token_fails(self) -> None:
        """Branch starting with docs/ and containing token should fail."""
        is_valid, msg = check_branch_name("docs/fix-FRE-1011-issue")
        assert is_valid is False
        assert "FRE-1011" in msg

    def test_console_branch_without_token_passes(self) -> None:
        """Console (close-out) branch without token should pass."""
        is_valid, msg = check_branch_name("console-kibana-retirement-directive")
        assert is_valid is True
        assert msg == ""

    def test_console_branch_with_token_fails(self) -> None:
        """Console branch with FRE token should fail."""
        is_valid, msg = check_branch_name("console-fre-1011-something")
        assert is_valid is False
        assert "fre-1011" in msg.lower()

    def test_session_branch_without_token_passes(self) -> None:
        """Session branch without token should pass."""
        is_valid, msg = check_branch_name("session-delta-2026-08-09")
        assert is_valid is True
        assert msg == ""

    def test_session_branch_with_token_fails(self) -> None:
        """Session branch with FRE token should fail."""
        is_valid, msg = check_branch_name("session-FRE-1011-delta")
        assert is_valid is False
        assert "FRE-1011" in msg

    def test_multiple_tokens_in_docs_branch_fails(self) -> None:
        """Docs branch with multiple FRE tokens should fail and report all."""
        is_valid, msg = check_branch_name("docs/fre-1011-and-FRE-1012")
        assert is_valid is False
        # Should mention both tokens in the error
        assert "FRE-1011" in msg or "FRE-1011" in msg.upper()
        assert "FRE-1012" in msg or "FRE-1012" in msg.upper()

    def test_case_insensitive_token_detection(self) -> None:
        """Token detection should be case-insensitive."""
        is_valid, msg = check_branch_name("docs/fre-1011-something")
        assert is_valid is False
        is_valid, msg = check_branch_name("docs/FRE-1011-something")
        assert is_valid is False


class TestCheckPrTitle:
    """Tests for PR title validation."""

    def test_delivering_branch_with_matching_token_in_title_passes(self) -> None:
        """PR title from a delivering branch with matching token should pass."""
        is_valid, msg = check_pr_title(
            "fix(FRE-1011): guard docs branches against ticket tokens",
            "fre-1011-trace-id-cost-ledger",
        )
        assert is_valid is True
        assert msg == ""

    def test_docs_branch_without_token_in_title_passes(self) -> None:
        """Docs PR without token in title should pass."""
        is_valid, msg = check_pr_title(
            "docs: update ADR index",
            "docs/adr-0125-accepted",
        )
        assert is_valid is True
        assert msg == ""

    def test_docs_branch_with_token_in_title_fails(self) -> None:
        """Docs PR with FRE token in title should fail."""
        is_valid, msg = check_pr_title(
            "docs(FRE-1011): guard docs branches",
            "docs/adr-0125-accepted",
        )
        assert is_valid is False
        assert "FRE-1011" in msg

    def test_mismatched_token_in_title_fails(self) -> None:
        """PR title with token not matching branch token should fail."""
        is_valid, msg = check_pr_title(
            "fix(FRE-1012): something",
            "fre-1011-trace-id-cost-ledger",
        )
        assert is_valid is False
        assert "FRE-1012" in msg

    def test_multiple_tokens_in_title_with_none_matching_branch_fails(self) -> None:
        """PR title with multiple tokens none matching branch should fail."""
        is_valid, msg = check_pr_title(
            "fix(FRE-1011): related to FRE-1012",
            "fre-1999-something-else",
        )
        assert is_valid is False

    def test_no_token_in_title_always_passes(self) -> None:
        """PR title with no token should always pass."""
        is_valid, msg = check_pr_title(
            "fix: update something",
            "docs/some-branch",
        )
        assert is_valid is True
        assert msg == ""

    def test_token_in_title_matching_branch_prefix_passes(self) -> None:
        """Token in title matching branch prefix should pass."""
        is_valid, msg = check_pr_title(
            "docs(FRE-1011): something",
            "fre-1011-trace-id-cost-ledger",
        )
        assert is_valid is True
        assert msg == ""


class TestCheckPrBodyWarning:
    """Tests for PR body warning (non-blocking)."""

    def test_docs_branch_with_token_in_body_warns(self) -> None:
        """Docs PR with token in body should generate a warning."""
        warning = check_pr_body_warning(
            "This closes FRE-1011 and updates the ADR.",
            "docs/adr-0125-accepted",
        )
        assert "FRE-1011" in warning
        assert "verify the ticket state" in warning

    def test_docs_branch_without_token_in_body_no_warning(self) -> None:
        """Docs PR without token in body should not warn."""
        warning = check_pr_body_warning(
            "This updates the documentation.",
            "docs/adr-0125-accepted",
        )
        assert warning == ""

    def test_delivering_branch_with_token_in_body_no_warning(self) -> None:
        """Delivering branch with token in body should not warn (expected)."""
        warning = check_pr_body_warning(
            "This closes FRE-1011.",
            "fre-1011-trace-id-cost-ledger",
        )
        assert warning == ""

    def test_empty_body_no_warning(self) -> None:
        """Empty PR body should not warn."""
        warning = check_pr_body_warning(None, "docs/adr-0125-accepted")
        assert warning == ""

    def test_docs_branch_with_multiple_tokens_in_body_warns(self) -> None:
        """Docs PR with multiple tokens in body should warn about all."""
        warning = check_pr_body_warning(
            "Closes FRE-1011. Related: FRE-1012, FRE-1013.",
            "docs/session-delta-2026-08-09",
        )
        assert "FRE-1011" in warning
        assert "FRE-1012" in warning
        assert "FRE-1013" in warning


class TestAcceptanceCriteria:
    """Tests that directly map to the ticket's acceptance criteria."""

    def test_ac1_reject_docs_branch_with_ticket_token(self) -> None:
        """AC-1: Reject a documentation branch carrying a ticket token."""
        is_valid, msg = check_branch_name("docs/fre-1011-something")
        assert is_valid is False, "Should reject docs branch with FRE token"

    def test_ac2_reject_pr_title_with_token_from_clean_docs_branch(self) -> None:
        """AC-2: Reject a PR whose title carries a token from a clean docs branch."""
        is_valid, msg = check_pr_title(
            "docs(FRE-1011): update something",
            "docs/adr-0125-accepted",
        )
        assert is_valid is False, "Should reject PR title with token from docs branch"

    def test_ac3_normal_delivering_branch_passes(self) -> None:
        """AC-3: Confirm a normal delivering branch passes unaffected."""
        is_valid, msg = check_branch_name("fre-1011-trace-id-cost-ledger")
        assert is_valid is True, "Delivering branch with token should pass"

        is_valid, msg = check_pr_title(
            "fix(FRE-1011): guard docs branches",
            "fre-1011-trace-id-cost-ledger",
        )
        assert is_valid is True, "PR title with matching branch token should pass"

    def test_ac4_docs_pr_body_reference_passes_with_warning(self) -> None:
        """AC-4: Docs PR referencing a ticket only in body passes with warning."""
        # Branch name check should pass
        is_valid, msg = check_branch_name("docs/adr-0125-accepted")
        assert is_valid is True, "Docs branch without token in name should pass"

        # Title check should pass (no token)
        is_valid, msg = check_pr_title(
            "docs: update ADR-0125",
            "docs/adr-0125-accepted",
        )
        assert is_valid is True, "Docs PR title without token should pass"

        # Body should generate a warning
        warning = check_pr_body_warning(
            "Related: FRE-1011. This updates ADR-0125.",
            "docs/adr-0125-accepted",
        )
        assert "FRE-1011" in warning, "Should warn about token in docs PR body"
        assert warning != "", "Should generate a warning"
