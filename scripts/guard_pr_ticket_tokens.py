#!/usr/bin/env python3
"""Guard against ticket tokens in docs branches and PR titles (FRE-1011).

This script enforces that:
1. Documentation branches (docs/*) must not contain FRE-XXXX tokens
2. PR titles must not contain FRE-XXXX tokens unless the source branch does

When used as a pre-commit hook, it checks the current branch name.
When called from GitHub Actions, it can check both branch and PR title.

Returns:
    0 if no violations; 1 if any match; 2 on environment errors.
"""

from __future__ import annotations

import re
import sys

# Pattern to match Linear ticket tokens (e.g., FRE-1011)
_FRE_TOKEN_PATTERN = re.compile(r"\bFRE-\d{3,4}\b", re.IGNORECASE)

# Prefixes that identify non-delivering branches (no FRE token allowed)
_DOCS_BRANCH_PREFIXES = ("docs/", "console-", "session-", "docs-", "session-delta")

# Close-out/master-specific branch prefixes
_CLOSEOUT_BRANCH_PREFIXES = ("console-", "master-")


def _extract_fre_tokens(text: str) -> list[str]:
    """Extract all FRE-XXXX tokens from text."""
    return _FRE_TOKEN_PATTERN.findall(text)


def _is_docs_branch(branch_name: str) -> bool:
    """Check if branch name matches a documentation/non-delivering prefix."""
    branch_lower = branch_name.lower()
    return any(branch_lower.startswith(prefix) for prefix in _DOCS_BRANCH_PREFIXES)


def _is_branch_carrying_token(branch_name: str, token: str) -> bool:
    """Check if the branch name itself contains the given FRE token."""
    return token.lower() in branch_name.lower()


def check_branch_name(branch_name: str) -> tuple[bool, str]:
    """Check if a branch name violates token rules.

    A docs branch must not contain any FRE-XXXX tokens.

    Args:
        branch_name: The git branch name to check.

    Returns:
        (is_valid, message) tuple.
    """
    if not _is_docs_branch(branch_name):
        # Non-docs branches are allowed to carry FRE tokens.
        return True, ""

    tokens = _extract_fre_tokens(branch_name)
    if tokens:
        return (
            False,
            f"Documentation branch '{branch_name}' must not contain FRE tokens. "
            f"Found: {', '.join(tokens)}. "
            f"Use a topic slug instead (e.g., 'docs/adr-0125-accepted').",
        )
    return True, ""


def check_pr_title(pr_title: str, branch_name: str) -> tuple[bool, str]:
    """Check if a PR title violates token rules.

    A PR title must not contain FRE-XXXX tokens unless the source branch carries that token.

    Args:
        pr_title: The pull request title.
        branch_name: The source branch name.

    Returns:
        (is_valid, message) tuple.
    """
    tokens = _extract_fre_tokens(pr_title)
    if not tokens:
        return True, ""

    # If branch carries the token, the title is allowed to reference it
    for token in tokens:
        if not _is_branch_carrying_token(branch_name, token):
            return (
                False,
                f"PR title must not contain FRE tokens unless the branch carries them. "
                f"Branch '{branch_name}' does not contain '{token}'. "
                f"Please remove FRE tokens from the PR title or ensure the branch name contains them.",
            )

    return True, ""


def check_pr_body_warning(pr_body: str | None, branch_name: str) -> str:
    """Generate a warning if PR body references a ticket from a docs branch.

    This is a warning only, not a hard failure. A docs branch referencing a ticket
    in the body is legitimate (e.g., "Closes FRE-1011" or "Related: FRE-1011"),
    but the user should verify the ticket state afterwards.

    Args:
        pr_body: The pull request body/description.
        branch_name: The source branch name.

    Returns:
        Warning message, or empty string if no warning needed.
    """
    if not pr_body or not _is_docs_branch(branch_name):
        return ""

    tokens = _extract_fre_tokens(pr_body)
    if tokens:
        return (
            f"⚠️  Documentation branch '{branch_name}' mentions ticket tokens in body: {', '.join(tokens)}. "
            f"Please verify the ticket state after merge — documentation PRs can inadvertently move tickets."
        )
    return ""


def main() -> None:
    """Entry point for pre-commit hook (checks branch name only)."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        branch_name = result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"guard_pr_ticket_tokens: cannot determine branch name: {e}", file=sys.stderr)
        sys.exit(2)

    is_valid, message = check_branch_name(branch_name)
    if not is_valid:
        print(message, file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
