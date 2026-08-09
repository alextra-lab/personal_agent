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
from tests._helpers.budget_config import budget_config_path


def test_real_repo_config_is_clean() -> None:
    """The committed configuration produces no coverage findings."""
    findings = check_budget_role_coverage(repo_root())
    assert findings == [], [str(f) for f in findings]


def _write_budget(tmp_path: Path, doc: dict[str, object], *, name: str = "budget.yaml") -> Path:
    governance = tmp_path / "config" / "governance"
    governance.mkdir(parents=True)
    (governance / name).write_text(yaml.safe_dump(doc), encoding="utf-8")
    return tmp_path


@pytest.fixture
def real_budget_doc() -> dict[str, object]:
    """The shipped budget config, parsed — perturbed per-test.

    Prefers the real (gitignored) ``budget.yaml`` and falls back to the
    committed template; both carry the role structure these tests perturb.
    """
    loaded = yaml.safe_load(budget_config_path().read_text(encoding="utf-8"))
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
    """Absent real file AND absent example is reported, not treated as clean."""
    findings = check_budget_role_coverage(tmp_path)
    assert len(findings) == 1
    assert findings[0].severity == "safety"
    assert "budget.yaml" in findings[0].message


# ── FRE-1238 AC-3: a fresh clone has only the .example, and the guard must
# VALIDATE it rather than skip. Both halves are required: a guard that returns
# "clean" for an example-only tree could be validating or could be skipping,
# and only the perturbed case tells the two apart.


def test_example_only_tree_is_clean(tmp_path: Path, real_budget_doc: dict[str, object]) -> None:
    """The fresh-clone shape passes: no real file, consistent example."""
    root = _write_budget(tmp_path, real_budget_doc, name="budget.yaml.example")

    assert check_budget_role_coverage(root) == []


def test_example_only_tree_is_actually_validated_not_skipped(
    tmp_path: Path, real_budget_doc: dict[str, object]
) -> None:
    """A drifted example still fails the guard — proving fallback validates.

    The discriminating half of AC-3. If the guard merely skipped when the real
    file is absent, this perturbed example would sail through and the check
    would have quietly stopped guarding anything.
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

    root = _write_budget(tmp_path, doc, name="budget.yaml.example")
    findings = check_budget_role_coverage(root)

    messages = " ".join(f.message for f in findings)
    assert "newly_added_role" in messages, "the example was skipped, not validated"
    assert all(f.severity == "safety" for f in findings)


def test_real_file_wins_over_example(tmp_path: Path, real_budget_doc: dict[str, object]) -> None:
    """With both present the real file is authoritative, not the template."""
    drifted = {
        **real_budget_doc,
        "roles": {
            **real_budget_doc["roles"],
            "ghost": {  # type: ignore[dict-item]
                "default_output_tokens": 256,
                "safety_factor": 1.2,
                "on_denial": "nack",
            },
        },
    }
    root = _write_budget(tmp_path, real_budget_doc)
    _write_budget_beside(root, drifted, name="budget.yaml.example")

    # The drift lives only in the example, which must be ignored while the real
    # file is present — so the guard sees a consistent config.
    assert check_budget_role_coverage(root) == []


def _write_budget_beside(root: Path, doc: dict[str, object], *, name: str) -> None:
    """Write a second governance file into an already-created tree."""
    (root / "config" / "governance" / name).write_text(yaml.safe_dump(doc), encoding="utf-8")
