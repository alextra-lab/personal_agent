"""FRE-1007 — the reasoning declaration is mandatory and verified against provider truth.

The guard does not compare a declared ``reasoning_effort`` against a hand-written
table of what each vendor accepts. It runs the declared value through **litellm's
own transformation** for that exact model, together with the deployment's other
declared parameters, and requires a real result. That single predicate is what
separates a wired field from an effective one:

* an effort litellm *drops* for a model is decorative — the request goes out bare
  and the provider default applies (``none`` on Anthropic);
* an effort litellm *rejects* for a model is an outage (any effort above ``none``
  beside ``gpt-5.4-mini``'s pinned ``temperature: 0.0``).

Both failure modes are real values that a reviewer would have accepted on sight,
which is why the guard asks the transformation rather than a human.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent.config.config_guard import (
    check_reasoning_declaration,
    run_all_checks,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _checks(root: Path, name: str) -> list[str]:
    """Return the messages of every finding named *name* under *root*."""
    return [f.message for f in check_reasoning_declaration(root) if f.check == name]


class TestProviderWireShape:
    """The §2 measurement table, pinned.

    This is the instrument the guard depends on. If litellm's mapping moves, this
    fails loudly rather than silently changing what the guard requires of us.
    """

    def test_sonnet_effort_becomes_adaptive_thinking_plus_output_config(self) -> None:
        from personal_agent.config.config_guard import reasoning_wire_shape

        shape, error = reasoning_wire_shape("claude-sonnet-5", "anthropic", {}, "high")
        assert error is None
        assert shape == {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
        }

    def test_openai_effort_becomes_a_flat_parameter(self) -> None:
        """Same declared field, a different wire shape — the ticket's core claim."""
        from personal_agent.config.config_guard import reasoning_wire_shape

        shape, error = reasoning_wire_shape("gpt-5.4-mini", "openai", {"temperature": 0.0}, "none")
        assert error is None
        assert shape["reasoning_effort"] == "none"
        assert "thinking" not in shape

    def test_none_is_dropped_on_anthropic_so_it_declares_nothing(self) -> None:
        """``none`` satisfies the letter of the rule while sending nothing."""
        from personal_agent.config.config_guard import reasoning_wire_shape

        shape, error = reasoning_wire_shape("claude-sonnet-5", "anthropic", {}, "none")
        assert error is None
        assert shape == {}

    def test_effort_beside_pinned_temperature_zero_is_rejected_by_the_provider(self) -> None:
        """The failure that would have been an outage, not a cost change."""
        from personal_agent.config.config_guard import reasoning_wire_shape

        _shape, error = reasoning_wire_shape(
            "gpt-5.4-mini", "openai", {"temperature": 0.0}, "medium"
        )
        assert error is not None
        assert "temperature" in error


class TestUnknownIsNotTheSameAsForbidden:
    """The bug that refused to boot the application, pinned as a regression.

    litellm's per-model capability flags come from a map fetched from GitHub at
    import, with a bundled fallback that does not list newer models — today it has
    no entry for ``claude-sonnet-5``. In that state litellm reports every reasoning
    parameter unsupported, ``thinking`` included. The first cut of this guard read
    that as "the provider forbids a declaration" and emitted
    ``reasoning_undeclarable_binding`` for every Anthropic producer, which made
    ``load_app_config()`` raise on any host whose egress reached Anthropic but not
    GitHub. "I cannot verify" must never become "you are wrong".
    """

    def test_an_unknown_model_reports_cannot_say_rather_than_unsupported(self) -> None:
        from personal_agent.llm_client.reasoning import provider_reasoning_support

        assert provider_reasoning_support("a-model-litellm-never-heard-of", "anthropic") is None

    def test_a_provider_with_no_lever_is_still_closed_without_a_third_check(self) -> None:
        """The loophole stays shut through the checks that ARE locally decidable.

        A role bound to a provider that carries no reasoning parameter cannot be
        caught by asking litellm (it reports the same "unsupported" for a model it
        has simply never seen). It does not need to be: with no declaration the
        binding fails `reasoning_declaration_missing` at safety class, and with one
        it fails `reasoning_declaration_rejected` in CI.
        """
        messages = _checks(_FIXTURES / "reasoning_undeclarable", "reasoning_declaration_missing")
        assert len(messages) == 1
        assert "qwen3.6-27b-ovh" in messages[0]

    def test_verification_findings_are_policy_so_they_never_block_boot(self) -> None:
        """Only locally-decidable facts may refuse a boot."""
        network_dependent = {
            "reasoning_declaration_rejected",
            "reasoning_declaration_ineffective",
        }
        for fixture in ("reasoning_ineffective", "reasoning_rejected"):
            for finding in check_reasoning_declaration(_FIXTURES / fixture):
                if finding.check in network_dependent:
                    assert finding.severity == "policy", finding

    def test_missing_declaration_stays_safety_so_it_does_block_boot(self) -> None:
        """The ticket's core demand survives the severity split."""
        findings = check_reasoning_declaration(_FIXTURES / "reasoning_undeclared")
        missing = [f for f in findings if f.check == "reasoning_declaration_missing"]
        assert missing and all(f.severity == "safety" for f in missing)


class TestTheTwoCompanionListsAgree:
    """The companion-field list is stated twice; this is what keeps it one list.

    ``config_guard`` restates it rather than importing it, so that reading the
    catalog never pulls the litellm SDK onto the settings-import path. Two
    literals can drift, so the guarantee has to be a test rather than a comment.
    """

    def test_config_guard_and_llm_client_declare_the_same_companions(self) -> None:
        from personal_agent.config.config_guard import _WIRE_COMPANION_FIELDS
        from personal_agent.llm_client.reasoning import WIRE_COMPANION_FIELDS

        assert _WIRE_COMPANION_FIELDS == WIRE_COMPANION_FIELDS


class TestRealRepoIsClean:
    """Every role-bound llm deployment declares, effectively, on the real repo."""

    def test_reasoning_check_is_clean(self) -> None:
        assert check_reasoning_declaration(_REPO_ROOT) == []

    def test_whole_guard_stays_clean(self) -> None:
        assert run_all_checks(_REPO_ROOT) == []


class TestSeededNegatives:
    """AC-1/AC-4 — proven by planting each violation and watching it fail."""

    def test_undeclared_bound_producer_fails(self) -> None:
        messages = _checks(_FIXTURES / "reasoning_undeclared", "reasoning_declaration_missing")
        assert len(messages) == 1
        assert "claude_sonnet" in messages[0]

    def test_decorative_declaration_fails(self) -> None:
        """``none`` on Anthropic — present in config, absent from the request."""
        messages = _checks(_FIXTURES / "reasoning_ineffective", "reasoning_declaration_ineffective")
        assert len(messages) == 1
        assert "sends nothing" in messages[0]

    def test_provider_rejected_declaration_fails(self) -> None:
        messages = _checks(_FIXTURES / "reasoning_rejected", "reasoning_declaration_rejected")
        assert len(messages) == 1
        assert "temperature" in messages[0]

    def test_thinking_disable_on_a_cloud_tool_model_fails(self) -> None:
        messages = _checks(
            _FIXTURES / "thinking_disable_tool_model", "thinking_disable_on_tool_model"
        )
        assert len(messages) == 1

    def test_local_qwen_thinking_disable_stays_legal(self) -> None:
        """The mechanism, not the misuse — the ticket scopes the prohibition itself."""
        assert not _checks(_REPO_ROOT, "thinking_disable_on_tool_model")

    def test_effort_on_a_local_deployment_is_a_vocabulary_mismatch(self) -> None:
        """Never sent on the local path — it would read as configured while doing nothing."""
        messages = _checks(
            _FIXTURES / "reasoning_vocabulary_mismatch", "reasoning_vocabulary_mismatch"
        )
        assert len(messages) == 1

    @pytest.mark.parametrize(
        "fixture",
        [
            "reasoning_undeclared",
            "reasoning_ineffective",
            "reasoning_rejected",
            "thinking_disable_tool_model",
            "reasoning_vocabulary_mismatch",
        ],
    )
    def test_cli_exits_nonzero_on_each_seeded_negative(self, fixture: str) -> None:
        from scripts.check_config import main

        assert main(["--root", str(_FIXTURES / fixture)]) == 1
