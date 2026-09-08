"""Tests for the sub-agent tool principal (FRE-1388).

AC-1: a sub-agent grant set exists separately from the primary's tool policy.
AC-2: a tool absent from the grant set is refused — default-deny, not a deny-list.
AC-3: a seeded negative (a real tool name outside the grant set) is refused, not
    a vacuous check against an empty request.
AC-4: covered at the orchestrator layer (test_expansion_controller.py) — a denial
    must reach the primary's report, not just a log line.
AC-5: an empty request changes nothing, preserving the pre-FRE-1388 status quo.
"""

from __future__ import annotations

from personal_agent.governance.models import GovernanceConfig, Mode, ToolPolicy
from personal_agent.governance.sub_agent_tools import (
    SUB_AGENT_DENIED_MODES,
    evaluate_sub_agent_tool_grant,
    sub_agent_tool_requires_approval,
)


def _config(sub_agent_tools: list[str]) -> GovernanceConfig:
    return GovernanceConfig(
        modes={},
        tools={},
        sub_agent_tools=sub_agent_tools,
        mode_constraints={},
    )


def _config_with_policy(tool_name: str, policy: ToolPolicy) -> GovernanceConfig:
    return GovernanceConfig(
        modes={},
        tools={tool_name: policy},
        sub_agent_tools=[tool_name],
        mode_constraints={},
    )


class TestEmptyRequestIsANoOp:
    """AC-5 — no tools requested means no behaviour change."""

    def test_empty_request_grants_and_denies_nothing(self) -> None:
        config = _config(["run_python"])
        grant = evaluate_sub_agent_tool_grant([], Mode.NORMAL, config)
        assert grant.granted == ()
        assert grant.denied == ()
        assert grant.denial_reason is None


class TestGrantSetIsDistinctFromThePrimary:
    """AC-1 — the sub-agent grant set is its own list, not a primary-policy flag."""

    def test_granted_tool_passes(self) -> None:
        config = _config(["run_python"])
        grant = evaluate_sub_agent_tool_grant(["run_python"], Mode.NORMAL, config)
        assert grant.granted == ("run_python",)
        assert grant.denied == ()

    def test_grant_set_is_read_from_its_own_config_field(self) -> None:
        # A tool granted to the primary (web_search would be, in NORMAL) but
        # absent from sub_agent_tools is still refused — the two lists are
        # independent, so this cannot pass by falling back to the primary's policy.
        config = _config(["run_python"])
        grant = evaluate_sub_agent_tool_grant(["web_search"], Mode.NORMAL, config)
        assert grant.granted == ()
        assert grant.denied == ("web_search",)


class TestSeededNegativeIsRefused:
    """AC-2 / AC-3 — a real, named tool outside the grant set is refused.

    Not a check against an empty tool list, which would pass vacuously (the
    project has been bitten by exactly that shape before).
    """

    def test_tool_outside_grant_set_is_denied(self) -> None:
        config = _config(["run_python"])
        grant = evaluate_sub_agent_tool_grant(["bash"], Mode.NORMAL, config)
        assert grant.granted == ()
        assert grant.denied == ("bash",)
        assert grant.denial_reason is not None
        assert "bash" in grant.denial_reason

    def test_mixed_request_splits_granted_and_denied(self) -> None:
        config = _config(["run_python"])
        grant = evaluate_sub_agent_tool_grant(["bash", "run_python"], Mode.NORMAL, config)
        assert grant.granted == ("run_python",)
        assert grant.denied == ("bash",)

    def test_default_is_deny_not_an_allow_list_with_exceptions(self) -> None:
        # An empty grant set (nothing configured) refuses every request —
        # the default must be deny, never permissive with a deny-list.
        config = _config([])
        grant = evaluate_sub_agent_tool_grant(["run_python"], Mode.NORMAL, config)
        assert grant.granted == ()
        assert grant.denied == ("run_python",)


class TestAlertAndDegradedDenyEverything:
    """Owner directive (2026-09-04): sub-agents hold no tools in ALERT or DEGRADED —

    a sub-agent runs unattended, so an approval-gated tool has no correct outcome
    there. This binds every grant, so it is checked before the grant-set lookup.
    """

    def test_denied_modes_are_exactly_alert_and_degraded(self) -> None:
        assert SUB_AGENT_DENIED_MODES == frozenset({Mode.ALERT, Mode.DEGRADED})

    def test_alert_denies_an_otherwise_granted_tool(self) -> None:
        config = _config(["run_python"])
        grant = evaluate_sub_agent_tool_grant(["run_python"], Mode.ALERT, config)
        assert grant.granted == ()
        assert grant.denied == ("run_python",)
        assert grant.denial_reason == "sub-agents hold no tools in ALERT mode"

    def test_degraded_denies_an_otherwise_granted_tool(self) -> None:
        config = _config(["run_python"])
        grant = evaluate_sub_agent_tool_grant(["run_python"], Mode.DEGRADED, config)
        assert grant.granted == ()
        assert grant.denied == ("run_python",)

    def test_normal_mode_is_unaffected(self) -> None:
        config = _config(["run_python"])
        grant = evaluate_sub_agent_tool_grant(["run_python"], Mode.NORMAL, config)
        assert grant.granted == ("run_python",)


class TestApprovalRequirementPredicate:
    """FRE-1461 — whether a granted tool needs the owner's word before it runs.

    The predicate must read the same two policy fields, in the same way, as the
    primary's own gate (``tools/executor.py``). A divergence here would mean the
    sub-agent asks about a different set of tools than the primary does.
    """

    def test_tool_with_no_policy_entry_needs_no_approval(self) -> None:
        config = _config(["run_python"])
        assert sub_agent_tool_requires_approval("run_python", Mode.NORMAL, config) is False

    def test_always_requires_approval_flag_is_honoured_in_every_mode(self) -> None:
        config = _config_with_policy(
            "run_python",
            ToolPolicy(
                category="compute",
                allowed_in_modes=["NORMAL"],
                requires_approval=True,
            ),
        )
        assert sub_agent_tool_requires_approval("run_python", Mode.NORMAL, config) is True

    def test_mode_listed_in_requires_approval_in_modes_needs_approval(self) -> None:
        config = _config_with_policy(
            "run_python",
            ToolPolicy(
                category="compute",
                allowed_in_modes=["NORMAL", "ALERT"],
                requires_approval_in_modes=["ALERT"],
            ),
        )
        assert sub_agent_tool_requires_approval("run_python", Mode.ALERT, config) is True

    def test_mode_outside_requires_approval_in_modes_needs_none(self) -> None:
        """The seeded negative — a real policy that simply does not name this mode."""
        config = _config_with_policy(
            "run_python",
            ToolPolicy(
                category="compute",
                allowed_in_modes=["NORMAL", "ALERT"],
                requires_approval_in_modes=["ALERT"],
            ),
        )
        assert sub_agent_tool_requires_approval("run_python", Mode.NORMAL, config) is False

    def test_shipped_run_python_policy_needs_no_approval_in_normal(self) -> None:
        """Against the real config: today's grant is inert in NORMAL (FRE-1461 §1).

        ``run_python`` requires approval in exactly ALERT and DEGRADED, which are
        exactly the modes where a sub-agent holds no tools at all. This asserts the
        circularity the ticket describes, so a later config edit that makes the
        mechanism live in NORMAL cannot pass unnoticed.
        """
        from personal_agent.config.governance_loader import load_governance_config

        config = load_governance_config()
        assert sub_agent_tool_requires_approval("run_python", Mode.NORMAL, config) is False
        assert sub_agent_tool_requires_approval("run_python", Mode.ALERT, config) is True
