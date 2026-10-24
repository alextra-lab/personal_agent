"""Thinking canary — does the thinking control reach the model? (FRE-1368 / ADR-0141 D6.1).

FRE-1007's declaration guard checks what the catalog **says**. It cannot see a control that
evaporates in transit: the OpenAI SDK once posted ``extra_body`` as a literal key, and every
sampler and thinking control was inert for months. This canary checks the behaviour, through
the production dispatch path, in two arms:

* **On arm (positive control).** The primary's default mode keeps thinking on. The reply
  **must** carry reasoning. Without this arm a zero-reasoning read on the off arm proves
  nothing: a client that discards reasoning, or a backend whose launch flag disables thinking,
  reads zero too.
* **Off arm.** The ``sub_agent`` binding's mode sends ``chat_template_kwargs.enable_thinking=false``
  to the same deployment. The reply **must not** carry reasoning.

Rules that keep it a monitor and not a second health writer:

* It never writes the SLM-health verdict (master, 2026-10-24). It returns a
  :class:`ThinkingCanaryResult`. The scheduler adds that result to the Elasticsearch copy of the
  snapshot after the verdict is cached (:mod:`.scheduler_runner`).
* A skip is not a verdict, and a dispatch error is not a verdict on the control. Only an arm
  that completed with the wrong reasoning reading is red.
* It skips when the probe-shared rules skip (:func:`.probe._resolve_probe_target`: a busy
  provider, a primary that is not the served model), and when the catalog cannot test the
  control (the declaration guard owns that failure).
* Both arms are bounded: a short timeout, no retry, background priority and a short slot wait.
  Priority only orders the waiters. A user turn that arrives while an arm holds a slot waits
  for that arm, for a few seconds in practice and 45 s at worst.
* It resolves the primary deployment from the catalog and uses the trusted-config factory door,
  which ignores the per-turn model selection. A user's selection cannot move the target.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from personal_agent.observability.slm_health.probe import _resolve_probe_target
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.llm_client.litellm_client import LiteLLMClient
    from personal_agent.llm_client.types import ModelRole
    from personal_agent.telemetry.trace import TraceContext

log = get_logger(__name__)

_PROMPT = "What is 17 + 25? Reply with the number only."
# Flash-Next spends about 20-23 reasoning tokens on a trivial prompt, so 256 leaves a wide
# margin for the on arm. The off arm needs only the answer. A reply cut at the cap still
# carries reasoning, so the on arm accepts ``finish_reason="length"``.
_ON_ARM_MAX_TOKENS = 256
_OFF_ARM_MAX_TOKENS = 64
# The scheduler awaits the tick, so two arms at this ceiling bound the worst case at 90 s.
_ARM_TIMEOUT_S = 45.0
_SLOT_WAIT_S = 2.0
_ERROR_MAX_LEN = 200


class ThinkingCanaryResult(BaseModel):
    """One canary run. ``ok`` is the only verdict; ``None`` means no verdict was reached.

    Attributes:
        ok: ``True`` when both arms read as expected. ``False`` when an arm completed with
            the wrong reading (red). ``None`` when the canary skipped or could not measure.
        skip_reason: Why it did not run. ``None`` when it ran.
        error: The red arm's description, or the dispatch error that stopped a measurement.
        on_reasoning_chars: Reasoning characters read on the thinking-on arm.
        off_reasoning_chars: Reasoning characters read on the thinking-off arm.
    """

    model_config = ConfigDict(frozen=True)

    ok: bool | None = None
    skip_reason: str | None = None
    error: str | None = None
    on_reasoning_chars: int | None = None
    off_reasoning_chars: int | None = None

    def snapshot_fields(self) -> dict[str, bool | str | int | None]:
        """Return this result under the ``SlmHealthSnapshot`` field names.

        Returns:
            A mapping for ``SlmHealthSnapshot.model_copy(update=...)``.
        """
        return {
            "thinking_canary_ok": self.ok,
            "thinking_canary_skip_reason": self.skip_reason,
            "thinking_canary_error": self.error,
            "thinking_canary_on_reasoning_chars": self.on_reasoning_chars,
            "thinking_canary_off_reasoning_chars": self.off_reasoning_chars,
        }


async def run_thinking_canary(*, base_url: str, trace_id: str) -> ThinkingCanaryResult:
    """Run both arms against the catalog's local primary and return the result.

    Args:
        base_url: The SLM's ``/v1`` API base, for the served-model lookup.
        trace_id: The probe tick's trace ID, for log correlation.

    Returns:
        A :class:`ThinkingCanaryResult`. Never raises.
    """
    try:
        result = await _run(base_url=base_url, trace_id=trace_id)
    except Exception as exc:  # noqa: BLE001 — a monitor must not break the tick
        result = ThinkingCanaryResult(error=f"{type(exc).__name__}: {exc}"[:_ERROR_MAX_LEN])
    _log_result(result, trace_id=trace_id)
    return result


async def _run(*, base_url: str, trace_id: str) -> ThinkingCanaryResult:
    """Resolve the target, check the catalog can test the control, and run the two arms."""
    from personal_agent.config.model_loader import load_model_config  # noqa: PLC0415
    from personal_agent.cost_gate import budget_role_for  # noqa: PLC0415
    from personal_agent.llm_client.factory import get_llm_client_for_key  # noqa: PLC0415
    from personal_agent.llm_client.models import INHERIT_DEPLOYMENT  # noqa: PLC0415
    from personal_agent.llm_client.types import ModelRole  # noqa: PLC0415
    from personal_agent.telemetry.trace import SystemTraceContext  # noqa: PLC0415

    target, skip_reason = await _resolve_probe_target(base_url=base_url, trace_id=trace_id)
    if target is None:
        return ThinkingCanaryResult(skip_reason=skip_reason)

    config = load_model_config()
    primary_binding = config.roles["primary"]
    primary_key = primary_binding.deployment
    primary_def = config.models[primary_key]

    if primary_def.resolve_mode().enable_thinking is not True:
        return ThinkingCanaryResult(
            skip_reason=(
                f"primary deployment {primary_key!r} default mode does not enable thinking, "
                "so the on arm has no reasoning to read"
            )
        )
    sub_agent = config.roles.get("sub_agent")
    if sub_agent is None:
        return ThinkingCanaryResult(skip_reason="catalog declares no 'sub_agent' role binding")
    if sub_agent.deployment not in (INHERIT_DEPLOYMENT, primary_key):
        return ThinkingCanaryResult(
            skip_reason=(
                f"sub_agent binds {sub_agent.deployment!r}, not the primary deployment "
                f"{primary_key!r}, so the arms would test two different backends"
            )
        )
    if sub_agent.mode is None:
        return ThinkingCanaryResult(
            skip_reason="sub_agent binding declares no mode, so no thinking-off control is sent"
        )
    off_mode = primary_def.modes.get(sub_agent.mode)
    if off_mode is None or off_mode.enable_thinking is not False:
        return ThinkingCanaryResult(
            skip_reason=(
                f"sub_agent mode {sub_agent.mode!r} does not disable thinking on "
                f"{primary_key!r}, so the off arm has no control to test"
            )
        )

    on_client = get_llm_client_for_key(primary_key, budget_role=budget_role_for("primary"))
    off_client = get_llm_client_for_key(
        primary_key, budget_role=budget_role_for("sub_agent"), mode=sub_agent.mode
    )
    trace_ctx = SystemTraceContext.new("slm_thinking_canary")

    on_chars, on_error = await _run_arm(
        on_client, ModelRole.PRIMARY, "on", _ON_ARM_MAX_TOKENS, trace_ctx
    )
    off_chars, off_error = await _run_arm(
        off_client, ModelRole.SUB_AGENT, "off", _OFF_ARM_MAX_TOKENS, trace_ctx
    )
    return _verdict(on_chars, off_chars, [e for e in (on_error, off_error) if e])


async def _run_arm(
    client: LiteLLMClient, role: ModelRole, label: str, max_tokens: int, trace_ctx: TraceContext
) -> tuple[int | None, str | None]:
    """Make one canary call and return ``(reasoning characters, error)``.

    Args:
        client: The :class:`~personal_agent.llm_client.litellm_client.LiteLLMClient` for the arm.
        role: The :class:`~personal_agent.llm_client.types.ModelRole` for telemetry.
        label: ``"on"`` or ``"off"``, for the callsite and the error text.
        max_tokens: The arm's completion cap.
        trace_ctx: The canary's system trace context.

    Returns:
        ``(chars, None)`` when the call completed. ``(None, <error>)`` when it did not.
    """
    from personal_agent.llm_client.concurrency import InferenceSlotTimeout  # noqa: PLC0415
    from personal_agent.llm_client.priority import InferencePriority  # noqa: PLC0415
    from personal_agent.llm_client.prompt_identity import (  # noqa: PLC0415
        derive_fallback_prompt_identity,
    )

    messages = [{"role": "user", "content": _PROMPT}]
    try:
        response = await client.respond(
            role=role,
            messages=messages,
            trace_ctx=trace_ctx,
            max_tokens=max_tokens,
            timeout_s=_ARM_TIMEOUT_S,
            max_retries=0,
            prompt_identity=derive_fallback_prompt_identity(
                f"slm_health.thinking_canary.{label}", system_prompt=None, request_messages=messages
            ),
            priority=InferencePriority.BACKGROUND,
            priority_timeout=_SLOT_WAIT_S,
        )
    except InferenceSlotTimeout:
        return None, f"{label} arm: no inference slot within {_SLOT_WAIT_S:.0f}s (backend busy)"
    except Exception as exc:  # noqa: BLE001 — a failed call measures nothing
        return None, f"{label} arm: {type(exc).__name__}: {exc}"[:_ERROR_MAX_LEN]
    return len(response.get("reasoning_trace") or ""), None


def _verdict(
    on_chars: int | None, off_chars: int | None, arm_errors: list[str]
) -> ThinkingCanaryResult:
    """Turn the two readings into a result. A wrong reading is red; a missing one is no verdict."""
    failures: list[str] = []
    if on_chars == 0:
        failures.append(
            "on arm: no reasoning read with thinking enabled (the positive control failed: "
            "the client discards reasoning, or the backend does not think)"
        )
    if off_chars:
        failures.append(
            f"off arm: {off_chars} reasoning characters read with thinking disabled "
            "(the disable control did not take effect)"
        )
    if failures:
        ok: bool | None = False
    elif on_chars is not None and off_chars is not None:
        ok = True
    else:
        ok = None
    error = "; ".join([*failures, *arm_errors]) or None
    return ThinkingCanaryResult(
        ok=ok, error=error, on_reasoning_chars=on_chars, off_reasoning_chars=off_chars
    )


def _log_result(result: ThinkingCanaryResult, *, trace_id: str) -> None:
    """Log the outcome. Red is a warning so it surfaces without the ES document."""
    fields: dict[str, Any] = {
        "ok": result.ok,
        "skip_reason": result.skip_reason,
        "error": result.error,
        "on_reasoning_chars": result.on_reasoning_chars,
        "off_reasoning_chars": result.off_reasoning_chars,
        "trace_id": trace_id,
        "component": "slm_health",
    }
    if result.ok is False:
        log.warning("slm_thinking_canary_red", **fields)
    else:
        log.info("slm_thinking_canary_completed", **fields)
