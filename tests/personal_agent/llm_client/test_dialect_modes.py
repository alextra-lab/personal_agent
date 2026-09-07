"""ADR-0145 D3a — dialect declaration and mode validation.

A dialect is a named wire vocabulary: the thinking lever, plus the accepted
sampling set. Declared on ``ProviderDefinition``, overridable on
``ModelDefinition``. Every ``kind: llm`` deployment must declare ``modes:``
in its resolved dialect's vocabulary, validated at catalog load — the same
place ``kind`` compatibility is validated today (``models.py``'s
``_bindings_are_valid_and_kind_compatible``).

AC-1/AC-2/AC-3 test the validator in isolation with small synthetic catalogs.
AC-4 asserts the eight real, migrated ``config/models.yaml`` entries carry the
exact pre-migration values in their default mode. AC-5 asserts the removed
fields are genuinely gone from ``ModelDefinition``, not merely defaulted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from personal_agent.config.model_loader import load_model_config
from personal_agent.llm_client.models import (
    ModelConfig,
    ModelDefinition,
    ModeSpec,
    ProviderDefinition,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CATALOG = _REPO_ROOT / "config" / "models.yaml"


def _config(*, provider_dialect: str | None, model_dialect: str | None, mode_body: dict) -> None:
    """Build and validate a one-model, one-provider catalog. Raises on failure."""
    ModelConfig(
        providers={
            "p": ProviderDefinition(
                placement="local" if provider_dialect == "llamacpp_qwen" else "cloud",
                max_concurrency=1,
                dialect=provider_dialect,
            )
        },
        models={
            "m": ModelDefinition(
                id="m",
                provider="p",
                dialect=model_dialect,
                context_length=8192,
                max_concurrency=1,
                default_timeout=30,
                modes={"default": ModeSpec.model_validate(mode_body)},
                default_mode="default",
            )
        },
    )


class TestAC1InvalidFieldFailsAtLoad:
    """AC-1 — a field the dialect does not accept fails at load, naming the field and dialect."""

    def test_llamacpp_qwen_rejects_reasoning_effort(self) -> None:
        """reasoning_effort validates and does nothing on this dialect (FRE-1430 F16) — excluded."""
        with pytest.raises(ValidationError, match="reasoning_effort.*llamacpp_qwen"):
            _config(
                provider_dialect="llamacpp_qwen",
                model_dialect=None,
                mode_body={"reasoning_effort": "low"},
            )

    def test_ovh_qwen_rejects_enable_thinking(self) -> None:
        """enable_thinking is llamacpp_qwen's lever, not OVH's (FRE-1430 F1)."""
        with pytest.raises(ValidationError, match="enable_thinking.*ovh_qwen"):
            _config(
                provider_dialect="ovh_qwen",
                model_dialect=None,
                mode_body={"enable_thinking": True},
            )

    def test_openai_gpt5_rejects_top_k(self) -> None:
        """top_k is unknown to OpenAI's gpt-5 family (FRE-1430 F1: 'Unknown parameter')."""
        with pytest.raises(ValidationError, match="top_k.*openai_gpt5"):
            _config(
                provider_dialect="openai_gpt5",
                model_dialect=None,
                mode_body={"top_k": 20},
            )

    def test_anthropic_adaptive_rejects_temperature(self) -> None:
        """Sonnet 5 rejects temperature as deprecated (FRE-1430 F1, F6)."""
        with pytest.raises(ValidationError, match="temperature.*anthropic_adaptive"):
            _config(
                provider_dialect=None,
                model_dialect="anthropic_adaptive",
                mode_body={"temperature": 0.5},
            )

    def test_anthropic_budget_rejects_effort(self) -> None:
        """Haiku 4.5 has no adaptive thinking and no effort parameter (FRE-1430 F6)."""
        with pytest.raises(ValidationError, match="effort.*anthropic_budget"):
            _config(
                provider_dialect=None,
                model_dialect="anthropic_budget",
                mode_body={"effort": "high"},
            )


class TestAC2InvalidValueFailsAtLoad:
    """AC-2 — a value outside the dialect's domain fails at load."""

    def test_ovh_qwen_rejects_xhigh(self) -> None:
        """The provider's own gateway 422s on xhigh (FRE-1430 F3) — the exact live rejection."""
        with pytest.raises(ValidationError, match="reasoning_effort=.xhigh.*ovh_qwen"):
            _config(
                provider_dialect="ovh_qwen",
                model_dialect=None,
                mode_body={"reasoning_effort": "xhigh"},
            )

    def test_ovh_qwen_rejects_high(self) -> None:
        """The gateway 400s on high too — only none/low/medium reach the model (FRE-1430 F3)."""
        with pytest.raises(ValidationError, match="reasoning_effort=.high.*ovh_qwen"):
            _config(
                provider_dialect="ovh_qwen",
                model_dialect=None,
                mode_body={"reasoning_effort": "high"},
            )

    def test_ovh_qwen_accepts_the_real_domain(self) -> None:
        """none/low/medium are the measured intersection that reaches the model (FRE-1430 F3)."""
        for value in ("none", "low", "medium"):
            _config(
                provider_dialect="ovh_qwen",
                model_dialect=None,
                mode_body={"reasoning_effort": value},
            )

    def test_openai_gpt5_accepts_the_full_ladder(self) -> None:
        """OpenAI's five-rung ladder has no hole (FRE-1430 F1)."""
        for value in ("none", "low", "medium", "high", "xhigh"):
            _config(
                provider_dialect="openai_gpt5",
                model_dialect=None,
                mode_body={"reasoning_effort": value},
            )


class TestCrossFieldRulesBeyondFieldMembership:
    """A field can individually belong to a dialect's accepted set and still be
    an illegal COMBINATION with another field the dialect also accepts
    (FRE-1430 F1/F5/F6) — caught by codex plan-review as a gap on the first
    pass of this validator.
    """

    def test_openai_gpt5_rejects_temperature_with_a_nonzero_effort(self) -> None:
        """OpenAI accepts temperature/top_p only at effort 'none' (F1, F5)."""
        with pytest.raises(ValidationError, match="temperature.*openai_gpt5|openai_gpt5.*low"):
            _config(
                provider_dialect="openai_gpt5",
                model_dialect=None,
                mode_body={"temperature": 0.5, "reasoning_effort": "low"},
            )

    def test_openai_gpt5_accepts_temperature_at_effort_none(self) -> None:
        _config(
            provider_dialect="openai_gpt5",
            model_dialect=None,
            mode_body={"temperature": 0.0, "reasoning_effort": "none"},
        )

    def test_anthropic_budget_rejects_temperature_with_top_p(self) -> None:
        """Haiku 4.5 400s if both are specified together (FRE-1430 F6)."""
        with pytest.raises(ValidationError, match="anthropic_budget"):
            _config(
                provider_dialect=None,
                model_dialect="anthropic_budget",
                mode_body={"temperature": 0.5, "top_p": 0.9},
            )

    def test_anthropic_budget_accepts_temperature_alone(self) -> None:
        _config(
            provider_dialect=None,
            model_dialect="anthropic_budget",
            mode_body={"temperature": 0.5},
        )

    def test_anthropic_budget_accepts_top_p_alone(self) -> None:
        _config(
            provider_dialect=None,
            model_dialect="anthropic_budget",
            mode_body={"top_p": 0.9},
        )


class TestAC3DefaultModeMustMatchADeclaredMode:
    """AC-3 — modes declared with no matching default_mode fails at load."""

    def test_default_mode_not_in_modes_fails(self) -> None:
        """default_mode naming a mode that doesn't exist fails at load."""
        with pytest.raises(ValidationError, match="default_mode"):
            ModelDefinition(
                id="m",
                context_length=8192,
                max_concurrency=1,
                default_timeout=30,
                modes={"thinking": ModeSpec()},
                default_mode="worker",
            )

    def test_modes_with_no_default_mode_fails(self) -> None:
        """Modes declared with no default_mode at all fails at load."""
        with pytest.raises(ValidationError, match="default_mode"):
            ModelDefinition(
                id="m",
                context_length=8192,
                max_concurrency=1,
                default_timeout=30,
                modes={"thinking": ModeSpec()},
            )

    def test_matching_default_mode_loads(self) -> None:
        """A default_mode naming a declared mode loads and resolves to it."""
        definition = ModelDefinition(
            id="m",
            context_length=8192,
            max_concurrency=1,
            default_timeout=30,
            modes={"thinking": ModeSpec(), "worker": ModeSpec()},
            default_mode="thinking",
        )
        assert definition.resolve_mode() is definition.modes["thinking"]

    def test_llm_deployment_with_no_modes_fails_in_a_real_catalog(self) -> None:
        """kind=llm requires modes; this rule lives on ModelConfig, which resolves the dialect."""
        with pytest.raises(ValidationError, match="modes"):
            ModelConfig(
                providers={
                    "p": ProviderDefinition(
                        placement="local", max_concurrency=1, dialect="llamacpp_qwen"
                    )
                },
                models={
                    "m": ModelDefinition(
                        id="m",
                        provider="p",
                        context_length=8192,
                        max_concurrency=1,
                        default_timeout=30,
                    )
                },
            )


class TestAC4RealCatalogEntriesAreBehaviourPreserving:
    """AC-4 — the migrated entries' default_mode equals their pre-migration values.

    Each expected value here is a literal copy of the value the entry declared
    before this migration (see the catalog's own inline comments for the
    pre-migration field name).

    ADR-0145 D1 (FRE-1445) later deleted the two `-instruct` entries; their
    two test methods below now assert the same literal preset survives as a
    `worker` mode on each entry's surviving local primary instead.
    """

    def test_qwen36_35b_thinking(self) -> None:
        """qwen3.6-35b-thinking's thinking-mode preset carries over unchanged."""
        mode = load_model_config(_CATALOG).models["qwen3.6-35b-thinking"].resolve_mode()
        assert mode.enable_thinking is True
        assert mode.temperature == 0.6
        assert mode.top_p == 0.95
        assert mode.top_k == 20
        assert mode.min_p == 0.0
        assert mode.presence_penalty == 0.0
        assert mode.repeat_penalty == 1.0

    def test_qwen38_flash_next(self) -> None:
        """qwen3.8-flash-next's thinking-mode preset carries over unchanged."""
        mode = load_model_config(_CATALOG).models["qwen3.8-flash-next"].resolve_mode()
        assert mode.enable_thinking is True
        assert mode.temperature == 1.0
        assert mode.top_p == 0.95
        assert mode.top_k == 20
        assert mode.min_p == 0.0
        assert mode.presence_penalty == 0.0
        assert mode.repeat_penalty == 1.0

    def test_qwen38_flash_next_worker_mode(self) -> None:
        """ADR-0145 D1 (FRE-1445) — qwen3.8-flash-next's `worker` mode carries the
        non-thinking preset the deleted qwen3.8-flash-next-instruct entry declared.
        """
        mode = load_model_config(_CATALOG).models["qwen3.8-flash-next"].resolve_mode("worker")
        assert mode.enable_thinking is False
        assert mode.temperature == 0.7
        assert mode.top_p == 0.8
        assert mode.top_k == 20
        assert mode.min_p == 0.0
        assert mode.presence_penalty == 1.5
        assert mode.repeat_penalty == 1.0

    def test_qwen36_35b_thinking_worker_mode(self) -> None:
        """ADR-0145 D1 (FRE-1445) — qwen3.6-35b-thinking's `worker` mode carries the
        non-thinking preset the deleted qwen3.6-35b-instruct entry declared.
        """
        mode = load_model_config(_CATALOG).models["qwen3.6-35b-thinking"].resolve_mode("worker")
        assert mode.enable_thinking is False
        assert mode.temperature == 0.7
        assert mode.top_p == 0.80
        assert mode.top_k == 20
        assert mode.min_p == 0.0
        assert mode.presence_penalty == 1.5
        assert mode.repeat_penalty == 1.0

    def test_qwen38_27b_ovh(self) -> None:
        """FRE-1441 (ADR-0145 D3b): now declares `medium`, closing the gap the
        reasoning guard left unwalked — the provider default (xhigh,
        unrequestable per FRE-1430 F3) no longer applies by omission.
        """
        mode = load_model_config(_CATALOG).models["qwen3.8-27b-ovh"].resolve_mode()
        assert mode.temperature == 1.0
        assert mode.reasoning_effort == "medium"

    def test_claude_sonnet(self) -> None:
        """claude_sonnet's reasoning_effort: high becomes effort: high, same value."""
        mode = load_model_config(_CATALOG).models["claude_sonnet"].resolve_mode()
        assert mode.effort == "high"

    def test_claude_haiku(self) -> None:
        """No sampler/thinking value was declared before this migration either."""
        mode = load_model_config(_CATALOG).models["claude_haiku"].resolve_mode()
        assert mode.model_dump(exclude_none=True) == {}

    def test_gpt_5_4_mini(self) -> None:
        """gpt-5.4-mini's temperature and reasoning_effort: none carry over unchanged."""
        mode = load_model_config(_CATALOG).models["gpt-5.4-mini"].resolve_mode()
        assert mode.temperature == 0.0
        assert mode.reasoning_effort == "none"


class TestAC5NoTopLevelSamplerOrThinkingFieldRemains:
    """AC-5 — the nine removed fields are genuinely gone from ModelDefinition, not defaulted.

    mypy cleanliness (the other half of AC-5) is enforced by `make mypy`, not
    exercisable from a runtime test.
    """

    _REMOVED_FIELDS = (
        "temperature",
        "top_p",
        "top_k",
        "presence_penalty",
        "min_p",
        "repetition_penalty",
        "reasoning_effort",
        "disable_thinking",
        "thinking_budget_tokens",
    )

    def test_fields_are_absent_from_the_schema(self) -> None:
        """None of the nine removed field names remain declared on ModelDefinition."""
        declared = set(ModelDefinition.model_fields)
        overlap = declared & set(self._REMOVED_FIELDS)
        assert not overlap, f"removed fields still declared on ModelDefinition: {overlap}"

    def test_a_removed_field_passed_at_construction_has_no_effect(self) -> None:
        """A caller passing the old field name is not rejected, but it goes nowhere.

        ModelDefinition carries no `extra="forbid"`, so the kwarg is silently
        dropped rather than raising — no attribute is created. mypy already
        refuses the kwarg statically (`make mypy`), which is the real backstop
        AC-5 asks for.
        """
        definition = ModelDefinition(
            id="m",
            context_length=8192,
            max_concurrency=1,
            default_timeout=30,
            temperature=0.5,  # type: ignore[call-arg]
        )
        assert not hasattr(definition, "temperature")


class TestEverySelectablePrimaryDeclaresAWorkerMode:
    """ADR-0145 D1 (FRE-1445), addressing master's 2026-09-07 comment on the ticket.

    `sub_agent`'s `deployment: inherit` binding means it now resolves onto
    WHATEVER the session's primary is, at its own `mode: worker`
    (`config/model_roles.yaml`). Master measured the consequence on a live
    turn (2026-09-07 05:12, OVH primary, 8 sub-agent calls): before this
    ticket the sub-agent ran free on the local model regardless of the
    primary's selection; after `inherit` lands, a cloud primary with no
    `worker` mode declared would fall back to that primary's own — expensive
    — `default_mode` (D2's fallback), silently billing every HYBRID
    sub-agent call at the primary's thinking depth.

    Rather than accept that window (master's option 3) or block this ticket
    on FRE-1448 landing first (master's option 1, which inverts the ADR's own
    dependency order), this folds ADR-0145 D6's already-measured per-dialect
    cheap-mode values (the D6 table in the ADR, not a new decision) onto
    every entry `role_candidates` can offer as `primary` — closing the
    window the same PR opens it (master's recommended option 2).
    """

    #: Every kind: llm entry in the real catalog, mirroring what
    #: `role_candidates` offers for the `open: true` `primary` role.
    _ALL_SELECTABLE_PRIMARIES: tuple[str, ...] = (
        "qwen3.6-35b-thinking",
        "qwen3.8-flash-next",
        "qwen3.8-27b-ovh",
        "claude_sonnet",
        "claude_haiku",
        "gpt-5.4-mini",
    )

    def test_every_selectable_primary_has_a_worker_mode(self) -> None:
        config = load_model_config(_CATALOG)
        missing = [
            key
            for key in self._ALL_SELECTABLE_PRIMARIES
            if "worker" not in config.models[key].modes
        ]
        assert not missing, (
            f"{missing} declare no `worker` mode — a primary selection landing "
            "there would route sub_agent onto that model's (likely more "
            "expensive) default_mode instead, via resolve_role_target's "
            "documented fallback-with-log (ADR-0145 D2)"
        )

    def test_sub_agent_resolves_a_worker_mode_for_every_primary_selection(self) -> None:
        """The resolver-level guarantee, not just the declaration: `mode: worker`
        actually lands on `worker`, never silently falls back to `default_mode`.
        """
        from personal_agent.config.model_loader import resolve_role_target

        config = load_model_config(_CATALOG)
        for primary_key in self._ALL_SELECTABLE_PRIMARIES:
            _, sub_def = resolve_role_target("sub_agent", model_key=primary_key, config=config)
            assert sub_def is not None
            assert sub_def.default_mode == "worker", (
                f"sub_agent inheriting {primary_key!r} as primary resolved to "
                f"default_mode={sub_def.default_mode!r}, not 'worker'"
            )

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("qwen3.8-27b-ovh", {"reasoning_effort": "none"}),
            ("claude_sonnet", {"effort": "low"}),
            ("claude_haiku", {}),
            ("gpt-5.4-mini", {"reasoning_effort": "none"}),
        ],
    )
    def test_cloud_worker_mode_matches_adr_0145_d6_table(
        self, key: str, expected: dict[str, object]
    ) -> None:
        """Each cloud primary's `worker` mode is D6's measured cheap value, verbatim."""
        mode = load_model_config(_CATALOG).models[key].resolve_mode("worker")
        for field, value in expected.items():
            assert getattr(mode, field) == value
        declared_fields = {f for f, v in mode.model_dump().items() if v is not None}
        assert declared_fields == set(expected), (
            f"{key}'s worker mode declares {declared_fields}, expected exactly {set(expected)}"
        )
