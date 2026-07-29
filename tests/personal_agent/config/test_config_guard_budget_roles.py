"""FRE-989 AC-1: the CI guard catches budget-role drift before it ships.

The ticket asks for "a test that fails when a role is added to one and not the
other". This is that test, at the guard layer — the one that runs in CI and
blocks the merge, rather than only in a unit test someone has to remember to
look at.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from personal_agent.config.config_guard import check_budget_role_coverage, repo_root


def test_real_repo_config_is_clean() -> None:
    """The committed configuration produces no coverage findings."""
    findings = check_budget_role_coverage(repo_root())
    assert findings == [], [str(f) for f in findings]


def _write_budget(tmp_path: Path, doc: dict[str, object]) -> Path:
    governance = tmp_path / "config" / "governance"
    governance.mkdir(parents=True)
    (governance / "budget.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return tmp_path


@pytest.fixture
def real_budget_doc() -> dict[str, object]:
    """The committed budget.yaml, parsed — perturbed per-test."""
    path = repo_root() / "config" / "governance" / "budget.yaml"
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_role_added_to_yaml_but_not_the_map_is_caught(
    tmp_path: Path, real_budget_doc: dict[str, object]
) -> None:
    """A new capped role with no resolver entry fails the build.

    This is exactly the ``study`` shape the audit found: declared and capped in
    YAML, absent from the map, therefore billed to ``main_inference``.
    """
    roles = dict(real_budget_doc["roles"])  # type: ignore[arg-type]
    roles["newly_added_role"] = {
        "default_output_tokens": 256,
        "safety_factor": 1.2,
        "on_denial": "nack",
    }
    caps = list(real_budget_doc["caps"])  # type: ignore[arg-type]
    caps.append({"time_window": "daily", "role": "newly_added_role", "cap_usd": 1.00})
    doc = {**real_budget_doc, "roles": roles, "caps": caps}

    findings = check_budget_role_coverage(_write_budget(tmp_path, doc))

    messages = " ".join(f.message for f in findings)
    assert "newly_added_role" in messages
    assert all(f.severity == "safety" for f in findings)


def test_role_with_neither_cap_nor_uncapped_declaration_is_caught(
    tmp_path: Path, real_budget_doc: dict[str, object]
) -> None:
    """Dropping a role's uncapped declaration surfaces the forgotten cap."""
    uncapped = [r for r in real_budget_doc["uncapped_roles"] if r != "insights"]  # type: ignore[union-attr]
    doc = {**real_budget_doc, "uncapped_roles": uncapped}

    findings = check_budget_role_coverage(_write_budget(tmp_path, doc))

    messages = " ".join(f.message for f in findings)
    assert "insights" in messages
    assert "uncapped_roles" in messages


def test_missing_budget_file_is_a_safety_finding(tmp_path: Path) -> None:
    """An absent budget.yaml is reported, not silently treated as clean."""
    findings = check_budget_role_coverage(tmp_path)
    assert len(findings) == 1
    assert findings[0].severity == "safety"
    assert "missing" in findings[0].message
