"""Tests for Linear ``save_issue`` argument normalization."""

from personal_agent.mcp.linear_issue_args import normalize_save_issue_arguments


def test_normalize_replaces_personalagent_team_alias() -> None:
    out = normalize_save_issue_arguments(
        {"title": "T", "team": "personalagent", "description": "x"},
        default_team="FrenchForest",
    )
    assert out["team"] == "FrenchForest"
    assert out["title"] == "T"


def test_normalize_preserves_real_team_name() -> None:
    out = normalize_save_issue_arguments(
        {"title": "T", "team": "FrenchForest"},
        default_team="FrenchForest",
    )
    assert out["team"] == "FrenchForest"


def test_normalize_maps_status_to_state() -> None:
    out = normalize_save_issue_arguments(
        {"title": "T", "team": "FrenchForest", "status": "Needs Approval"},
        default_team="FrenchForest",
    )
    assert out["state"] == "Needs Approval"
    assert "status" not in out


def test_normalize_prefers_state_over_status() -> None:
    out = normalize_save_issue_arguments(
        {
            "title": "T",
            "team": "FrenchForest",
            "state": "Approved",
            "status": "ignored",
        },
        default_team="FrenchForest",
    )
    assert out["state"] == "Approved"
    assert "status" not in out


def test_normalize_adds_default_team_when_missing_on_create() -> None:
    out = normalize_save_issue_arguments(
        {"title": "T", "description": "x"},
        default_team="FrenchForest",
    )
    assert out["team"] == "FrenchForest"


def test_normalize_does_not_add_team_on_update() -> None:
    out = normalize_save_issue_arguments(
        {"id": "FF-1", "state": "Done"},
        default_team="FrenchForest",
    )
    assert "team" not in out


def test_normalize_coerces_string_priority() -> None:
    out = normalize_save_issue_arguments(
        {"title": "T", "team": "FrenchForest", "priority": "2"},
        default_team="FrenchForest",
    )
    assert out["priority"] == 2
