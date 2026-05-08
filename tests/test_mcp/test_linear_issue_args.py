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


def test_normalize_replaces_uuid_team_with_default() -> None:
    """LLM passes raw team UUID (e.g. from a previous get_issue call) — must replace with name."""
    uuid_team = "e04acc02-94ee-4ab8-a2c8-a13f2b929655"
    out = normalize_save_issue_arguments(
        {"title": "T", "team": uuid_team, "description": "x"},
        default_team="FrenchForest",
    )
    assert out["team"] == "FrenchForest"


def test_normalize_replaces_uppercase_uuid_team() -> None:
    """UUID matching is case-insensitive."""
    uuid_team = "E04ACC02-94EE-4AB8-A2C8-A13F2B929655"
    out = normalize_save_issue_arguments(
        {"title": "T", "team": uuid_team},
        default_team="FrenchForest",
    )
    assert out["team"] == "FrenchForest"


# ── Label normalization (FRE-309) ─────────────────────────────────────────────

_PA_LABEL_ID = "25004aac-3b32-4fa4-bdc2-55ff348ea842"
_KNOWN_LABELS = {"PersonalAgent": _PA_LABEL_ID}


def test_normalize_replaces_personalagent_label_with_uuid() -> None:
    """Known label name is replaced with its UUID so MCP server skips name lookup."""
    out = normalize_save_issue_arguments(
        {"title": "T", "team": "FrenchForest", "labels": ["PersonalAgent", "Bug"]},
        default_team="FrenchForest",
        known_label_ids=_KNOWN_LABELS,
    )
    assert out["labels"] == [_PA_LABEL_ID, "Bug"]


def test_normalize_label_lookup_is_case_insensitive() -> None:
    """Label name match ignores case."""
    out = normalize_save_issue_arguments(
        {"title": "T", "team": "FrenchForest", "labels": ["personalagent"]},
        default_team="FrenchForest",
        known_label_ids=_KNOWN_LABELS,
    )
    assert out["labels"] == [_PA_LABEL_ID]


def test_normalize_unknown_labels_pass_through() -> None:
    """Labels not in the map are left unchanged for the MCP server to handle."""
    out = normalize_save_issue_arguments(
        {"title": "T", "team": "FrenchForest", "labels": ["Bug", "Tier-1:Opus"]},
        default_team="FrenchForest",
        known_label_ids=_KNOWN_LABELS,
    )
    assert out["labels"] == ["Bug", "Tier-1:Opus"]


def test_normalize_no_known_label_ids_leaves_labels_unchanged() -> None:
    """Omitting known_label_ids leaves labels untouched (backward compatibility)."""
    out = normalize_save_issue_arguments(
        {"title": "T", "team": "FrenchForest", "labels": ["PersonalAgent"]},
        default_team="FrenchForest",
    )
    assert out["labels"] == ["PersonalAgent"]


def test_normalize_labels_absent_is_a_noop() -> None:
    """No ``labels`` key in arguments → no error, no new key added."""
    out = normalize_save_issue_arguments(
        {"title": "T", "team": "FrenchForest"},
        default_team="FrenchForest",
        known_label_ids=_KNOWN_LABELS,
    )
    assert "labels" not in out
