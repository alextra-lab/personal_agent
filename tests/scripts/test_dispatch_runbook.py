# ruff: noqa: D103
"""AC-c: the dispatch runbook exists and states the master-unchanged invariant.

The acceptance criterion is literal — "the runbook exists and states the
master-unchanged invariant in plain terms" — so this asserts the file is present
and carries that invariant plus the enable-once precondition.
"""

from __future__ import annotations

from pathlib import Path

_RUNBOOK = Path("docs/runbooks/dispatch-orchestrator.md")


def test_runbook_exists() -> None:
    assert _RUNBOOK.is_file()


def test_runbook_states_master_unchanged_invariant() -> None:
    # Collapse whitespace so prose line-wrapping does not hide the invariant.
    text = " ".join(_RUNBOOK.read_text().lower().split())
    # dispatch-only, never merges/deploys/closes/edits MASTER_PLAN
    assert "dispatch only" in text
    assert "never merges, deploys, closes tickets, or edits master_plan" in text
    assert "both approval gates" in text


def test_runbook_documents_enable_once_precondition() -> None:
    text = _RUNBOOK.read_text().lower()
    assert "enable-once precondition" in text
    assert "claude auth login" in text
    assert "anthropic_base_url" in text
    assert "kill switch" in text
