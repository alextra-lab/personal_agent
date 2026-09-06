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
    """AC-4 — the eight migrated entries' default_mode equals their pre-migration values.

    Each expected value here is a literal copy of the value the entry declared
    before this migration (see the catalog's own inline comments for the
    pre-migration field name).
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

    def test_qwen38_flash_next_instruct(self) -> None:
        """qwen3.8-flash-next-instruct's non-thinking preset carries over unchanged."""
        mode = load_model_config(_CATALOG).models["qwen3.8-flash-next-instruct"].resolve_mode()
        assert mode.enable_thinking is False
        assert mode.temperature == 0.7
        assert mode.top_p == 0.8
        assert mode.top_k == 20
        assert mode.min_p == 0.0
        assert mode.presence_penalty == 1.5
        assert mode.repeat_penalty == 1.0

    def test_qwen36_35b_instruct(self) -> None:
        """qwen3.6-35b-instruct's non-thinking preset carries over unchanged."""
        mode = load_model_config(_CATALOG).models["qwen3.6-35b-instruct"].resolve_mode()
        assert mode.enable_thinking is False
        assert mode.temperature == 0.7
        assert mode.top_p == 0.80
        assert mode.top_k == 20
        assert mode.min_p == 0.0
        assert mode.presence_penalty == 1.5
        assert mode.repeat_penalty == 1.0

    def test_qwen38_27b_ovh(self) -> None:
        """No reasoning_effort before or after.

        The provider default (xhigh, unrequestable per FRE-1430 F3) still
        applies by omission, unchanged.
        """
        mode = load_model_config(_CATALOG).models["qwen3.8-27b-ovh"].resolve_mode()
        assert mode.temperature == 1.0
        assert mode.reasoning_effort is None

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
