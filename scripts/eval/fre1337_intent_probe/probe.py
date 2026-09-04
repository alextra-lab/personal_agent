"""Arm 2 — ask a model to classify a fixture against the injected taxonomy.

Contamination-free by construction (AC-2/AC-3): a single ``respond()`` call with no
``tools`` and no message history beyond the one user turn built by
:func:`taxonomy.build_probe_prompt`. There is nothing for ``search_memory`` — or any other
tool — to be, because no tool definitions are ever passed.

Model identity (codex plan-review finding, 2026-08-30 — historical; the underlying gap
this worked around no longer exists): before ADR-0141 D1, ``get_llm_client_for_key`` for a
**local** deployment returned a bare, role-agnostic ``LocalLLMClient`` — the requested key
was discarded, and ``respond(role=...)`` re-resolved the model from the per-turn selection
context (empty for a standalone script), falling through to whatever
``config/model_roles.yaml``'s binding for that role said (FRE-1343). ADR-0141 D1 dissolved
this by construction: every placement now dispatches through ``LiteLLMClient``, whose model
is fixed at construction regardless of placement. The ``set_current_selection`` pin below is
therefore now belt-and-suspenders rather than a required workaround, and is left in place —
``resolve_role_target`` still honors an explicit ``model_key`` unconditionally, so it remains
correct, just no longer load-bearing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

import structlog
from scripts.eval.fre1337_intent_probe.taxonomy import PROBE_SYSTEM_PROMPT, build_probe_prompt

from personal_agent.config import load_model_config
from personal_agent.config.selection import reset_current_selection, set_current_selection
from personal_agent.llm_client.factory import get_llm_client_for_key
from personal_agent.llm_client.types import ModelRole
from personal_agent.request_gateway.types import TaskType
from personal_agent.telemetry.trace import SystemTraceContext

log = structlog.get_logger(__name__)

#: The three primaries FRE-1337 asks to compare (ticket "What to build").
MODEL_KEYS: tuple[str, ...] = ("qwen3.6-35b-thinking", "qwen3.6-27b-ovh", "claude_sonnet")

_VALID_TASK_TYPES = {member.value for member in TaskType}


@dataclass(frozen=True)
class ModelClassification:
    """One model's classification of one fixture (AC-2 evidence row).

    Attributes:
        model_key: The requested deployment key.
        task_type: The model's answer, or ``"invalid_response"`` if unparseable /
            outside the taxonomy — never silently coerced to a guess.
        reason: The model's stated one-sentence reason.
        prompt: The verbatim system + user prompt sent (AC-2: "recorded verbatim in the
            output").
        raw_content: The model's raw response text, for debugging a parse failure.
        resolved_model_id: The model id echoed back by the backend
            (``raw["model"]``), when available — the model-identity check.
        requested_model_id: The catalog id for ``model_key``, to compare against
            ``resolved_model_id``.
    """

    model_key: str
    task_type: str
    reason: str
    prompt: str
    raw_content: str
    resolved_model_id: str | None
    requested_model_id: str


def _parse_response(content: str) -> tuple[str, str]:
    """Parse the model's JSON classification, fence-stripped.

    Args:
        content: Raw model response text.

    Returns:
        ``(task_type, reason)``. ``task_type`` is ``"invalid_response"`` when the content
        isn't valid JSON, lacks ``task_type``, or names a value outside the taxonomy.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
        task_type = str(parsed["task_type"])
        reason = str(parsed.get("reason", ""))
    except (json.JSONDecodeError, KeyError, TypeError):
        return "invalid_response", content
    if task_type not in _VALID_TASK_TYPES:
        return "invalid_response", content
    return task_type, reason


async def classify_with_model(
    model_key: str, message: str, *, trace_id: str | None = None
) -> ModelClassification:
    """Run the classification probe for one (model, fixture) pair.

    Args:
        model_key: Catalog deployment key (see :data:`MODEL_KEYS`).
        message: The fixture's user message text.
        trace_id: Optional trace id for telemetry correlation; a fresh one is minted
            when omitted.

    Returns:
        The parsed classification, with the verbatim prompt and model-identity fields.

    Raises:
        ValueError: If ``model_key`` is not registered in ``models.yaml``.
    """
    models = load_model_config().models
    if model_key not in models:
        raise ValueError(f"Unknown model key {model_key!r}. Available: {sorted(models)}")
    model_def = models[model_key]
    prompt = build_probe_prompt(message)

    token = set_current_selection({ModelRole.STUDY.value: model_key})
    try:
        client = get_llm_client_for_key(model_key, budget_role="study")
        response = await client.respond(
            role=ModelRole.STUDY,
            messages=[{"role": "user", "content": prompt}],
            system_prompt=PROBE_SYSTEM_PROMPT,
            trace_ctx=SystemTraceContext.new(
                "fre1337_intent_probe", session_id=trace_id or str(uuid4())
            ),
        )
    finally:
        reset_current_selection(token)

    content = str(response.get("content", ""))
    task_type, reason = _parse_response(content)
    raw = response.get("raw") or {}
    resolved_model_id = raw.get("model") if isinstance(raw, dict) else None

    return ModelClassification(
        model_key=model_key,
        task_type=task_type,
        reason=reason,
        prompt=f"{PROBE_SYSTEM_PROMPT}\n\n{prompt}",
        raw_content=content,
        resolved_model_id=resolved_model_id,
        requested_model_id=str(model_def.id),
    )
