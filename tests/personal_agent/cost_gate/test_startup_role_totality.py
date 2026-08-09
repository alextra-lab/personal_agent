"""FRE-989: role totality is validated at startup, not only at CI.

CI checks the tree; ``config/governance/budget.yaml`` is a *runtime* file that
can differ from the tree CI validated (it is baked into the gateway image — the
same trap as models.cloud.yaml under ADR-0121). A drifted deploy must refuse to
start rather than mis-bill silently for the life of the container.

Each test injects a self-consistent role map alongside a deliberately perturbed
budget config, so exactly one invariant is under test at a time. The real
committed config is checked, unperturbed, by :func:`test_real_config_passes`.
"""

from __future__ import annotations

import pytest

from personal_agent.cost_gate import (
    BudgetConfigError,
    validate_role_totality,
)
from personal_agent.cost_gate.types import BudgetConfig, CapEntry, RoleConfig
from personal_agent.llm_client.types import ModelRole
from tests._helpers.budget_config import load_budget_config_for_tests

_ROLE = RoleConfig(default_output_tokens=256, safety_factor=1.2, on_denial="nack")

# Every ModelRole member must be accounted for, or the first invariant fires and
# drowns out the one each test is actually pinning.
_ALL_MODEL_ROLES_NON_GATED = frozenset(role.value for role in ModelRole)


def _check(
    roles: dict[str, RoleConfig],
    caps: list[CapEntry],
    uncapped: list[str] | None = None,
    role_map: dict[str, str] | None = None,
) -> None:
    """Validate a synthetic config against a map that self-resolves its roles."""
    config = BudgetConfig(roles=roles, caps=caps, uncapped_roles=uncapped or [])
    validate_role_totality(
        config,
        role_map=role_map if role_map is not None else {name: name for name in roles},
        non_gated=_ALL_MODEL_ROLES_NON_GATED,
    )


def test_real_config_passes() -> None:
    """The shipped budget config, role map and ModelRole all agree.

    Reads the real ``budget.yaml`` where it exists (a dev machine, the VPS) and
    the committed ``budget.yaml.example`` otherwise — the real file is
    gitignored (FRE-1209). Both carry the same role structure, which is the
    invariant here; the caps themselves are not read.
    """
    validate_role_totality(load_budget_config_for_tests())  # must not raise


def test_declared_role_missing_from_map_raises() -> None:
    """A budget.yaml role with no self-resolving map entry fails startup.

    This is the ``study`` shape: capped in YAML with its own $5 isolation lane,
    absent from the resolver, therefore billed to main_inference instead.
    """
    with pytest.raises(BudgetConfigError, match="orphan_role"):
        _check(
            roles={"main_inference": _ROLE, "orphan_role": _ROLE},
            caps=[
                CapEntry(time_window="daily", role="main_inference", cap_usd="10.00"),
                CapEntry(time_window="daily", role="orphan_role", cap_usd="1.00"),
            ],
            role_map={"main_inference": "main_inference"},  # orphan_role absent
        )


def test_map_entry_pointing_at_undeclared_lane_raises() -> None:
    """A map entry may not name a budget lane budget.yaml does not declare."""
    with pytest.raises(BudgetConfigError, match="ghost_lane"):
        _check(
            roles={"main_inference": _ROLE},
            caps=[CapEntry(time_window="daily", role="main_inference", cap_usd="10.00")],
            role_map={"main_inference": "main_inference", "some_alias": "ghost_lane"},
        )


def test_role_with_no_cap_and_no_uncapped_declaration_raises() -> None:
    """Forgetting a cap must be distinguishable from deciding not to have one."""
    with pytest.raises(BudgetConfigError, match="silently_uncapped"):
        _check(
            roles={"main_inference": _ROLE, "silently_uncapped": _ROLE},
            caps=[CapEntry(time_window="daily", role="main_inference", cap_usd="10.00")],
        )


def test_role_declared_uncapped_is_accepted() -> None:
    """An explicitly declared uncapped role is a recorded decision, not a defect."""
    _check(
        roles={"main_inference": _ROLE, "deliberately_uncapped": _ROLE},
        caps=[CapEntry(time_window="daily", role="main_inference", cap_usd="10.00")],
        uncapped=["deliberately_uncapped"],
    )  # must not raise


def test_uncapped_declaration_for_an_undeclared_role_raises() -> None:
    """The uncapped list must not accumulate names of roles that no longer exist."""
    with pytest.raises(BudgetConfigError, match="a_role_that_was_deleted"):
        _check(
            roles={"main_inference": _ROLE},
            caps=[CapEntry(time_window="daily", role="main_inference", cap_usd="10.00")],
            uncapped=["a_role_that_was_deleted"],
        )


def test_role_both_capped_and_declared_uncapped_raises() -> None:
    """A role cannot be both capped and declared uncapped — drift, not a decision."""
    with pytest.raises(BudgetConfigError, match="both capped and listed"):
        _check(
            roles={"main_inference": _ROLE},
            caps=[CapEntry(time_window="daily", role="main_inference", cap_usd="10.00")],
            uncapped=["main_inference"],
        )


def test_unaccounted_model_role_member_raises() -> None:
    """A ModelRole member with neither a lane nor a non-gated declaration fails."""
    config = BudgetConfig(
        roles={"main_inference": _ROLE},
        caps=[CapEntry(time_window="daily", role="main_inference", cap_usd="10.00")],
    )
    with pytest.raises(BudgetConfigError, match="no budget lane"):
        validate_role_totality(
            config,
            role_map={"main_inference": "main_inference"},
            non_gated=frozenset(),  # nothing accounted for → every member is a finding
        )
