"""Tests for ADR-0040 Linear feedback loop helpers."""

import pathlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.captains_log.feedback import (
    FeedbackPoller,
    FeedbackRecord,
    _load_poller_state,
    _PollerState,
    _save_poller_state,
)
from personal_agent.captains_log.linear_client import extract_issue_identifier_from_description
from personal_agent.captains_log.suppression import (
    is_fingerprint_suppressed,
    record_rejection_suppression,
    suppression_file_path,
)
from personal_agent.insights.engine import InsightsEngine


def test_extract_fingerprint_from_html_comment() -> None:
    """Parse fingerprint from HTML comment in issue body."""
    desc = "Hello\n<!-- fingerprint: abcd1234ef567890 -->\n"
    assert extract_issue_identifier_from_description(desc) == "abcd1234ef567890"


def test_extract_fingerprint_from_markdown() -> None:
    """Parse fingerprint from bold Markdown line."""
    desc = "x\n\n**Fingerprint**: `DEADBEEF`\n"
    assert extract_issue_identifier_from_description(desc) == "deadbeef"


def test_suppression_roundtrip(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rejected fingerprints are stored and read back for suppression."""
    monkeypatch.setattr(
        "personal_agent.captains_log.suppression.feedback_history_dir", lambda: tmp_path
    )
    fp = "a" * 16
    assert not is_fingerprint_suppressed(fp)
    record_rejection_suppression(fp, issue_identifier="FF-1", duration_days=30)
    assert is_fingerprint_suppressed(fp)
    assert suppression_file_path().is_file()


def test_poller_state_save_load(tmp_path: pathlib.Path) -> None:
    """Poller state round-trips through JSON."""
    p = tmp_path / "state.json"
    s = _PollerState(handled={"id1": ["Approved"]})
    _save_poller_state(p, s)
    loaded = _load_poller_state(p)
    assert loaded.handled["id1"] == ["Approved"]


@pytest.mark.asyncio
async def test_feedback_poller_emits_new_label(tmp_path: pathlib.Path) -> None:
    """Poller detects unhandled Approved label and runs handler."""
    state_path = tmp_path / "poller.json"
    client = MagicMock()
    client.list_issues = AsyncMock(
        return_value=[{"id": "uuid-1", "identifier": "FF-1", "updatedAt": "2026-04-01T00:00:00Z"}]
    )
    client.get_issue = AsyncMock(
        return_value={
            "id": "uuid-1",
            "identifier": "FF-1",
            "title": "t",
            "labels": [{"name": "PersonalAgent"}, {"name": "Approved"}],
            "updatedAt": "2026-04-01T00:00:00Z",
        }
    )
    client.count_non_archived_issues = AsyncMock(return_value=10)

    poller = FeedbackPoller(client, state_path=state_path)
    events = await poller.check_for_feedback()
    assert len(events) == 1
    assert events[0].label == "Approved"

    client.update_issue = AsyncMock()
    await poller.process_feedback(events)
    client.update_issue.assert_called_once()
    loaded = _load_poller_state(state_path)
    assert "Approved" in loaded.handled.get("uuid-1", [])


@pytest.mark.asyncio
async def test_analyze_feedback_patterns(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Insights engine reads feedback_history JSON files."""
    monkeypatch.setattr(
        "personal_agent.insights.engine.feedback_history_dir", lambda: tmp_path
    )
    now = datetime.now(timezone.utc)
    rec = FeedbackRecord(
        issue_id="x",
        issue_identifier="FF-9",
        title="t",
        category="observability",
        feedback_label="Rejected",
        feedback_date=now,
        original_description="d",
    )
    (tmp_path / "FF-9.json").write_text(rec.model_dump_json())
    engine = InsightsEngine()
    insights = await engine.analyze_feedback_patterns(days=30)
    assert len(insights) >= 1
    assert any(i.insight_type == "feedback_summary" for i in insights)
