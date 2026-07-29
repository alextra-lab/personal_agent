"""FRE-989 AC-1/AC-2: budget-role resolution is total and fail-closed.

The defect these pin: ``budget_role_for`` used to fall back to
``main_inference`` for any name it did not recognise. A silent default is
indistinguishable from a correct mapping at every downstream layer — the
counters, the ledger and the telemetry all record the *wrong* lane with full
confidence. FRE-1037 widened ``ModelRole`` from 4 members to 14 without adding
every mapping, so the fallback's blast radius tripled while this sat approved.

Pure-function tests, no DB. The integration counterpart (a real reservation
landing on the right counter row) lives in ``test_role_lane_isolation.py``.
"""

from __future__ import annotations

import pytest

from personal_agent.cost_gate import (
    BUDGET_ROLE_BY_FACTORY_NAME,
    NON_GATED_ROLES,
    UnknownBudgetRoleError,
    budget_role_for,
    load_budget_config,
)
from personal_agent.llm_client.types import ModelRole


def test_model_role_members_all_mapped() -> None:
    """Every ModelRole member resolves to a budget lane or is declared non-gated."""
    unmapped = sorted(
        role.value
        for role in ModelRole
        if role.value not in BUDGET_ROLE_BY_FACTORY_NAME and role.value not in NON_GATED_ROLES
    )
    assert not unmapped, (
        f"ModelRole members with no budget lane and no non-gated declaration: {unmapped}. "
        "Add an entry to BUDGET_ROLE_BY_FACTORY_NAME or to NON_GATED_ROLES "
        "(src/personal_agent/cost_gate/role_map.py)."
    )


def test_budget_yaml_roles_all_self_resolve() -> None:
    """Every role declared in budget.yaml is reachable by its own name.

    This is the direction that would have caught ``study``: it was declared in
    budget.yaml with its own $5 daily cap, but absent from the resolver map, so
    ``budget_role_for("study")`` returned ``main_inference``.
    """
    declared = sorted(load_budget_config().roles)
    unreachable = [name for name in declared if BUDGET_ROLE_BY_FACTORY_NAME.get(name) != name]
    assert not unreachable, (
        f"budget.yaml roles that do not self-resolve through the map: {unreachable}"
    )


def test_map_targets_are_declared_budget_roles() -> None:
    """No map entry points at a budget lane budget.yaml does not declare."""
    declared = set(load_budget_config().roles)
    dangling = {k: v for k, v in BUDGET_ROLE_BY_FACTORY_NAME.items() if v not in declared}
    assert not dangling, f"map entries pointing at undeclared budget roles: {dangling}"


def test_non_gated_roles_are_not_also_mapped() -> None:
    """A role is either gated-with-a-lane or declared non-gated — never both."""
    both = sorted(NON_GATED_ROLES & set(BUDGET_ROLE_BY_FACTORY_NAME))
    assert not both, f"roles declared both non-gated and mapped to a budget lane: {both}"


def test_study_has_its_own_lane() -> None:
    """FRE-989 finding one, the residue FRE-1037 left behind.

    ``study``'s $5 daily / $7 weekly isolation (FRE-839) exists so a one-time
    corpus run can never contend with live extraction. Resolving it to
    ``main_inference`` defeated exactly that.
    """
    assert budget_role_for("study") == "study"


def test_unknown_role_name_raises() -> None:
    """AC-2: no silent fallback — an unmapped name is a loud failure."""
    with pytest.raises(UnknownBudgetRoleError):
        budget_role_for("definitely_not_a_declared_role")


def test_unknown_role_error_names_the_remedy() -> None:
    """The raise must tell the next reader where to fix it, not just that it broke."""
    with pytest.raises(UnknownBudgetRoleError) as excinfo:
        budget_role_for("definitely_not_a_declared_role")
    message = str(excinfo.value)
    assert "definitely_not_a_declared_role" in message
    assert "BUDGET_ROLE_BY_FACTORY_NAME" in message
    assert "NON_GATED_ROLES" in message


@pytest.mark.parametrize(
    ("factory_name", "expected_lane"),
    [
        ("primary", "main_inference"),
        ("sub_agent", "main_inference"),
        ("compressor", "main_inference"),
        ("artifact_builder", "artifact_builder"),
        ("captains_log", "captains_log"),
        ("captains_log_role", "captains_log"),
        ("session_summary", "captains_log"),
        ("skill_routing", "skill_routing"),
        ("study", "study"),
        ("entity_extraction", "entity_extraction"),
        ("entity_extraction_role", "entity_extraction"),
        ("insights", "insights"),
        ("vision", "main_inference"),
    ],
)
def test_known_names_resolve_deterministically(factory_name: str, expected_lane: str) -> None:
    """Every live factory role name resolves to one declared lane, no fallback."""
    assert budget_role_for(factory_name) == expected_lane
