"""What a declared reasoning effort actually becomes on the wire (FRE-1007).

Lives under ``llm_client/`` because it touches the litellm SDK, which the
topology guard confines here (``tests/observability/topology/test_ci_teeth.py``).
The config guard imports it lazily rather than importing litellm itself: the
architectural rule is that provider SDKs stay behind this package's seam, and a
guard is not a good reason to make an exception to it.

Despite the SDK dependency this makes **no** model call and needs no credentials
— it drives litellm's parameter transformation and nothing else, so it is safe in
CI, in pre-commit, and on the startup path.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager


@contextmanager
def _quiet_litellm() -> Iterator[None]:
    """Silence litellm's per-call parameter dump for the duration of a probe.

    Both entry points here run once per bound deployment on every pre-commit and
    every boot. A guard whose findings arrive buried in a vendor's debug output
    does not get read, which defeats the point of having one.
    """
    import litellm  # noqa: PLC0415 — keep the SDK off the config-import path

    litellm.suppress_debug_info = True
    verbose = logging.getLogger("LiteLLM")
    previous_level = verbose.level
    verbose.setLevel(logging.WARNING)
    try:
        yield
    finally:
        verbose.setLevel(previous_level)


#: Deployment fields forwarded to litellm alongside the effort, which can make an
#: otherwise-valid effort illegal. ``temperature`` is the live case: ``gpt-5.4-mini``
#: pins ``temperature: 0.0`` (FRE-758) and litellm rejects that together with any
#: effort above ``none`` on the gpt-5 family.
WIRE_COMPANION_FIELDS: tuple[str, ...] = ("temperature",)


def provider_reasoning_support(model_id: str, provider: str) -> bool | None:
    """Whether litellm can carry a reasoning effort for this model — or cannot say.

    The three-valued return is the whole point. litellm's per-model capability
    flags come from a **model cost map fetched from GitHub at import**, with a
    bundled JSON as fallback. Newer models are routinely absent from the bundled
    copy — ``claude-sonnet-5`` is absent today — and in that state litellm reports
    every reasoning parameter as unsupported, ``thinking`` included.

    So "litellm says no" and "litellm has never heard of this model" are different
    facts that look identical at the call site, and a guard that conflates them
    converts *I don't know* into *you are wrong*. Doing that refused to boot the
    application on a host whose egress reaches the provider but not GitHub.

    Args:
        model_id: The provider's own model id.
        provider: The litellm provider name.

    Returns:
        ``True`` when litellm knows the model and forwards ``reasoning_effort``
        for it; ``False`` when it knows the model and does not (litellm's
        ``ovhcloud`` provider); ``None`` when litellm holds no capability record,
        so nothing can be concluded either way.
    """
    import litellm  # noqa: PLC0415 — keep the SDK off the config-import path

    known = bool(litellm.model_cost.get(model_id)) or bool(
        litellm.model_cost.get(f"{provider}/{model_id}")
    )
    if not known:
        return None
    with _quiet_litellm():
        supported = litellm.get_supported_openai_params(
            model=model_id, custom_llm_provider=provider
        )
    return "reasoning_effort" in (supported or ())


def reasoning_wire_shape(
    model_id: str,
    provider: str,
    companion_params: Mapping[str, object],
    effort: str | None,
) -> tuple[dict[str, object], str | None]:
    """Ask litellm what a declared reasoning effort becomes for this exact model.

    This is the instrument behind
    :func:`~personal_agent.config.config_guard.check_reasoning_declaration`, and
    the reason that check is not a table of vendor rules. What an effort becomes
    is a property of the **model**, not the vendor: ``claude-sonnet-5`` advertises
    ``supports_output_config`` and maps effort onto an adaptive-thinking block,
    while ``claude-haiku-4-5`` advertises only ``supports_reasoning`` and takes
    litellm's legacy path, converting effort into an explicit thinking budget
    *and rewriting max_tokens*. Asking the transformation is the only way to know
    which, and both of the wrong answers it prevents (a value litellm silently
    drops, a value it rejects outright) look identical in review.

    Args:
        model_id: The provider's own model id (e.g. ``"claude-sonnet-5"``).
        provider: The litellm provider name (e.g. ``"anthropic"``).
        companion_params: Other declared parameters forwarded on the same call
            (see :data:`WIRE_COMPANION_FIELDS`), because they can make an
            otherwise-valid effort illegal.
        effort: The declared effort, or ``None`` to probe the undeclared baseline.

    Returns:
        ``(transformed_params, error_message)``. Exactly one is meaningful: on a
        provider rejection the dict is empty and the message is set; otherwise the
        message is ``None`` and the dict is what litellm would forward — empty
        when litellm drops the value entirely, which is the "declared but sends
        nothing" case.
    """
    from litellm.utils import get_optional_params  # noqa: PLC0415

    # Companions are passed by NAME rather than splatted: ``get_optional_params``
    # is precisely typed per parameter, so a ``**dict[str, object]`` splat fails
    # type checking against every overload. Naming them keeps the call typed and
    # keeps :data:`WIRE_COMPANION_FIELDS` — which the guard reads the deployment
    # with — as the one list to extend if a second companion ever matters.
    raw_temperature = companion_params.get("temperature")
    temperature = float(raw_temperature) if isinstance(raw_temperature, (int, float)) else None

    with _quiet_litellm():
        try:
            shape = get_optional_params(
                model=model_id,
                custom_llm_provider=provider,
                temperature=temperature,
                reasoning_effort=effort,
            )
        except Exception as exc:  # noqa: BLE001 — any provider rejection is a finding
            return {}, str(exc)
    return dict(shape), None
