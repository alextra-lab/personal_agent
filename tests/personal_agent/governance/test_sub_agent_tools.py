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

import pytest
from pydantic import ValidationError

from personal_agent.governance.models import (
    GovernanceConfig,
    Mode,
    SubAgentToolDecision,
    ToolPolicy,
)
from personal_agent.governance.sub_agent_tools import (
    SUB_AGENT_DENIED_MODES,
    ParamClamp,
    clamp_sub_agent_tool_params,
    evaluate_sub_agent_tool_grant,
    sub_agent_tool_requires_approval,
)


def _granted(*tool_names: str) -> dict[str, SubAgentToolDecision]:
    """Build a decision mapping granting exactly ``tool_names``."""
    return {
        name: SubAgentToolDecision(granted=True, reason="granted for this test")
        for name in tool_names
    }


def _config(sub_agent_tools: list[str]) -> GovernanceConfig:
    return GovernanceConfig(
        modes={},
        tools={},
        sub_agent_tools=_granted(*sub_agent_tools),
        mode_constraints={},
    )


def _config_with_decisions(decisions: dict[str, SubAgentToolDecision]) -> GovernanceConfig:
    return GovernanceConfig(
        modes={},
        tools={},
        sub_agent_tools=decisions,
        mode_constraints={},
    )


def _config_with_policy(tool_name: str, policy: ToolPolicy) -> GovernanceConfig:
    return GovernanceConfig(
        modes={},
        tools={tool_name: policy},
        sub_agent_tools=_granted(tool_name),
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


# --------------------------------------------------------------------------------------
# FRE-1463 — the grant is a per-tool decision record, not a list of names.
# --------------------------------------------------------------------------------------


class TestADecisionRecordCarriesItsReason:
    """FRE-1463 AC-1 — a refusal is an entry with a reason, not an absence."""

    def test_a_refused_entry_is_not_granted(self) -> None:
        config = _config_with_decisions(
            {
                "run_python": SubAgentToolDecision(granted=True, reason="the 2026-09-04 grant"),
                "fetch_url": SubAgentToolDecision(granted=False, reason="the 2026-09-08 refusal"),
            }
        )
        assert config.granted_sub_agent_tool_names() == ("run_python",)

    def test_a_refused_entry_is_denied_at_evaluation(self) -> None:
        """The leak this shape can introduce: a key read as a grant.

        Iterating the mapping would grant ``fetch_url`` because it is a key. The
        evaluation must read the granted subset, not the keys.
        """
        config = _config_with_decisions(
            {"fetch_url": SubAgentToolDecision(granted=False, reason="refused on 2026-09-08")}
        )
        grant = evaluate_sub_agent_tool_grant(["fetch_url"], Mode.NORMAL, config)
        assert grant.granted == ()
        assert grant.denied == ("fetch_url",)

    def test_an_explicit_refusal_surfaces_its_recorded_reason(self) -> None:
        """AC-3 — the denial names the tool AND says why it was refused."""
        config = _config_with_decisions(
            {"fetch_url": SubAgentToolDecision(granted=False, reason="FRE-1360 is still open")}
        )
        grant = evaluate_sub_agent_tool_grant(["fetch_url"], Mode.NORMAL, config)
        assert grant.denial_reason is not None
        assert "fetch_url" in grant.denial_reason
        assert "FRE-1360 is still open" in grant.denial_reason

    def test_a_tool_with_no_entry_is_still_denied_and_still_named(self) -> None:
        """AC-3's seeded negative — absence keeps working, with no recorded reason to add."""
        config = _config(["run_python"])
        grant = evaluate_sub_agent_tool_grant(["bash"], Mode.NORMAL, config)
        assert grant.denied == ("bash",)
        assert grant.denial_reason is not None
        assert "bash" in grant.denial_reason

    def test_a_blank_reason_is_rejected(self) -> None:
        """A reason that may be blank is a list wearing a mapping's clothes."""
        with pytest.raises(ValidationError):
            SubAgentToolDecision(granted=False, reason="   ")


class TestTheShippedDecisions:
    """FRE-1463 AC-1 — the four deferred tools each carry their own answer."""

    @pytest.mark.parametrize(
        ("tool_name", "expected_granted"),
        [
            ("run_python", True),
            ("web_search", True),
            ("search_memory", True),
            ("fetch_url", False),
            # FRE-1467 reversed this one: FRE-1463 refused it because it could
            # not work without an identity, and FRE-1467 threaded the identity.
            ("recall_personal_history", True),
        ],
    )
    def test_each_tool_has_its_own_decision_and_a_reason(
        self, tool_name: str, expected_granted: bool
    ) -> None:
        from personal_agent.config.governance_loader import load_governance_config

        config = load_governance_config()
        decision = config.sub_agent_tools.get(tool_name)
        assert decision is not None, f"{tool_name} has no recorded decision"
        assert decision.granted is expected_granted
        assert decision.reason.strip()

    def test_the_four_are_not_one_decision(self) -> None:
        """*Fails if* the four are granted as a block (the ticket's own words)."""
        from personal_agent.config.governance_loader import load_governance_config

        config = load_governance_config()
        four = ("web_search", "search_memory", "fetch_url", "recall_personal_history")
        answers = {config.sub_agent_tools[name].granted for name in four}
        assert answers == {True, False}

    def test_every_recorded_reason_is_distinct(self) -> None:
        """A reason copied across entries is a block decision in disguise."""
        from personal_agent.config.governance_loader import load_governance_config

        config = load_governance_config()
        reasons = [d.reason.strip() for d in config.sub_agent_tools.values()]
        assert len(set(reasons)) == len(reasons)


class TestAlertAndDegradedStillRevokeEveryNewGrant:
    """FRE-1463 AC-5 — the FRE-1388 revocation binds this grant too."""

    @pytest.mark.parametrize(
        "tool_name",
        ["web_search", "search_memory", "run_python", "recall_personal_history"],
    )
    @pytest.mark.parametrize("mode", [Mode.ALERT, Mode.DEGRADED])
    def test_a_newly_granted_tool_is_revoked_in_the_denied_modes(
        self, tool_name: str, mode: Mode
    ) -> None:
        from personal_agent.config.governance_loader import load_governance_config

        config = load_governance_config()
        assert tool_name in config.granted_sub_agent_tool_names()
        grant = evaluate_sub_agent_tool_grant([tool_name], mode, config)
        assert grant.granted == ()
        assert grant.denied == (tool_name,)
        assert grant.denial_reason == f"sub-agents hold no tools in {mode.value} mode"

    def test_the_newly_granted_tools_do_reach_a_sub_agent_in_normal(self) -> None:
        """AC-2's config half — permission, which the live probe then turns into use."""
        from personal_agent.config.governance_loader import load_governance_config

        config = load_governance_config()
        grant = evaluate_sub_agent_tool_grant(["web_search", "search_memory"], Mode.NORMAL, config)
        assert grant.granted == ("web_search", "search_memory")
        assert grant.denied == ()


# --------------------------------------------------------------------------------------
# FRE-1473 — per-principal parameter ceilings for recall_personal_history.
# --------------------------------------------------------------------------------------


def _decision_with_ceilings(**ceilings: int) -> SubAgentToolDecision:
    return SubAgentToolDecision(granted=True, reason="test decision", param_ceilings=ceilings)


def _config_with_decision(tool_name: str, decision: SubAgentToolDecision) -> GovernanceConfig:
    return GovernanceConfig(
        modes={},
        tools={},
        sub_agent_tools={tool_name: decision},
        mode_constraints={},
    )


class TestParamCeilingSchema:
    """FRE-1473 — ``SubAgentToolDecision.param_ceilings`` validates its own shape."""

    def test_default_is_empty(self) -> None:
        decision = SubAgentToolDecision(granted=True, reason="no ceilings recorded")
        assert decision.param_ceilings == {}

    def test_a_blank_key_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SubAgentToolDecision(granted=True, reason="x", param_ceilings={"  ": 10})

    def test_a_non_positive_ceiling_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SubAgentToolDecision(granted=True, reason="x", param_ceilings={"days_ago": 0})


class TestClampSubAgentToolParams:
    """FRE-1473 AC-1/AC-4/AC-5 — the clamp function itself, independent of dispatch."""

    def test_out_of_range_request_is_clamped_not_rejected(self) -> None:
        """AC-1 — an over-ceiling value is reduced; the call still produces usable arguments."""
        config = _config_with_decision(
            "recall_personal_history", _decision_with_ceilings(days_ago=30, limit=10)
        )
        clamped, applied = clamp_sub_agent_tool_params(
            "recall_personal_history", {"days_ago": 365, "limit": 50}, config
        )
        assert clamped == {"days_ago": 30, "limit": 10}
        assert applied == (
            ParamClamp(param="days_ago", requested=365, applied=30),
            ParamClamp(param="limit", requested=50, applied=10),
        )

    def test_in_bounds_request_is_unaffected(self) -> None:
        """AC-5 — the seeded negative: a within-ceiling request changes nothing."""
        config = _config_with_decision(
            "recall_personal_history", _decision_with_ceilings(days_ago=30, limit=10)
        )
        arguments = {"days_ago": 5, "topic": "athens"}
        clamped, applied = clamp_sub_agent_tool_params("recall_personal_history", arguments, config)
        assert clamped == arguments
        assert clamped is not arguments
        assert applied == ()

    def test_a_param_absent_from_the_ceiling_map_is_left_untouched(self) -> None:
        """Only the declared parameters are ever clamped, even when others are out of range."""
        config = _config_with_decision(
            "recall_personal_history", _decision_with_ceilings(days_ago=30)
        )
        clamped, applied = clamp_sub_agent_tool_params(
            "recall_personal_history", {"days_ago": 365, "limit": 999}, config
        )
        assert clamped == {"days_ago": 30, "limit": 999}
        assert applied == (ParamClamp(param="days_ago", requested=365, applied=30),)

    def test_a_tool_with_no_recorded_decision_is_a_no_op(self) -> None:
        config = _config_with_decision(
            "recall_personal_history", _decision_with_ceilings(days_ago=30)
        )
        clamped, applied = clamp_sub_agent_tool_params("web_search", {"days_ago": 365}, config)
        assert clamped == {"days_ago": 365}
        assert applied == ()

    def test_a_decision_with_no_ceilings_is_a_no_op(self) -> None:
        config = _config_with_decision(
            "recall_personal_history", SubAgentToolDecision(granted=True, reason="no ceilings")
        )
        clamped, applied = clamp_sub_agent_tool_params(
            "recall_personal_history", {"days_ago": 365}, config
        )
        assert clamped == {"days_ago": 365}
        assert applied == ()

    def test_the_input_mapping_is_never_mutated(self) -> None:
        config = _config_with_decision(
            "recall_personal_history", _decision_with_ceilings(days_ago=30)
        )
        arguments = {"days_ago": 365}
        clamp_sub_agent_tool_params("recall_personal_history", arguments, config)
        assert arguments == {"days_ago": 365}

    def test_a_non_numeric_or_boolean_value_is_left_for_the_tools_own_validation(self) -> None:
        """A malformed value is not this function's problem — it passes through unclamped."""
        config = _config_with_decision(
            "recall_personal_history", _decision_with_ceilings(days_ago=30)
        )
        clamped, applied = clamp_sub_agent_tool_params(
            "recall_personal_history", {"days_ago": "a lot"}, config
        )
        assert clamped == {"days_ago": "a lot"}
        assert applied == ()

        clamped_bool, applied_bool = clamp_sub_agent_tool_params(
            "recall_personal_history", {"days_ago": True}, config
        )
        assert clamped_bool == {"days_ago": True}
        assert applied_bool == ()


class TestShippedRecallPersonalHistoryCeiling:
    """FRE-1473 AC-4 — the ceiling is declared beside the grant it constrains."""

    def test_the_shipped_config_carries_the_recommended_ceiling(self) -> None:
        from personal_agent.config.governance_loader import load_governance_config

        config = load_governance_config()
        decision = config.sub_agent_tools["recall_personal_history"]
        assert decision.granted is True
        assert decision.param_ceilings == {"days_ago": 30, "limit": 10}
        assert "FRE-1473" in decision.reason

    def test_the_primarys_own_tool_policy_is_untouched(self) -> None:
        """AC-3's config half — no ceiling concept exists in the primary's own policy."""
        from personal_agent.config.governance_loader import load_governance_config

        config = load_governance_config()
        policy = config.tools["recall_personal_history"]
        assert not hasattr(policy, "param_ceilings")
