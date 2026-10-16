"""Tests for the FRE-1610 replay of canceled-ticket outcomes (AC-4)."""

from __future__ import annotations

import json
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts.fre1610_replay_cancellations import TICKETS, replay

from personal_agent.captains_log.suppression import is_fingerprint_suppressed


def _issue(ident: str, state: str, fp: str | None) -> dict:
    marker = f"\n<!-- fingerprint: {fp} -->" if fp else ""
    return {
        "identifier": ident,
        "state": {"name": state},
        "labels": [{"name": "PersonalAgent"}, {"name": "agent-filed"}],
        "description": f"## Proposed Change{marker}",
    }


def _fp(i: int) -> str:
    return f"{i:016x}"


@pytest.fixture
def history_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    monkeypatch.setattr(
        "personal_agent.captains_log.suppression.feedback_history_dir", lambda: tmp_path
    )
    return tmp_path


def _client(issues: dict[str, dict]) -> MagicMock:
    client = MagicMock()
    client.get_issue = AsyncMock(side_effect=lambda ident: issues[ident])
    return client


@pytest.mark.asyncio
async def test_dry_run_reports_every_canceled_ticket_and_writes_nothing(
    history_dir: pathlib.Path,
) -> None:
    """AC-4: all six unlabelled cancels would suppress; a dry run changes no file."""
    issues = {t: _issue(t, "Canceled", _fp(i + 1)) for i, t in enumerate(TICKETS)}

    rows = await replay(_client(issues), TICKETS, apply=False)

    assert [r.ticket for r in rows] == list(TICKETS)
    assert all(r.suppressed for r in rows)
    assert all(r.outcome == "canceled-as-noise" for r in rows)
    assert not any(is_fingerprint_suppressed(_fp(i + 1)) for i in range(len(TICKETS)))
    assert list(history_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_apply_writes_the_suppression(history_dir: pathlib.Path) -> None:
    issues = {t: _issue(t, "Canceled", _fp(i + 1)) for i, t in enumerate(TICKETS)}

    await replay(_client(issues), TICKETS, apply=True)

    assert all(is_fingerprint_suppressed(_fp(i + 1)) for i in range(len(TICKETS)))


@pytest.mark.asyncio
async def test_a_ticket_that_would_not_suppress_is_reported(history_dir: pathlib.Path) -> None:
    issues = {t: _issue(t, "Canceled", _fp(i + 1)) for i, t in enumerate(TICKETS)}
    issues[TICKETS[0]] = _issue(TICKETS[0], "Done", _fp(1))
    issues[TICKETS[1]] = _issue(TICKETS[1], "Canceled", None)

    rows = await replay(_client(issues), TICKETS, apply=False)

    assert [r.suppressed for r in rows] == [False, False, True, True, True, True]


def test_the_six_real_tickets_are_named() -> None:
    assert TICKETS == ("FRE-1373", "FRE-1378", "FRE-1404", "FRE-1412", "FRE-1418", "FRE-1429")


def test_json_fixture_shape_is_accepted(tmp_path: pathlib.Path) -> None:
    """--issues-json lets a seat without a Linear key replay exported tickets."""
    from scripts.fre1610_replay_cancellations import load_issues_json

    path = tmp_path / "issues.json"
    path.write_text(json.dumps([_issue("FRE-1", "Canceled", _fp(9))]))

    assert list(load_issues_json(path)) == ["FRE-1"]
