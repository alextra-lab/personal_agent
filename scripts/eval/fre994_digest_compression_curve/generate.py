"""One generation call, and the mechanical classification of its reply (FRE-994 §4.1).

The delivery endpoint is read entirely from what this module records, and it costs no
scoring calls: rendered length, whether the digest carried content at all, whether it
rendered inside its arm's stated bound, whether the reply was cut off, and the split of
billed output into content and structure.

The producer is never called. This dispatches the model directly with the production
prompt and the production contract, so no session is marked clean, nothing is written to
any substrate, and ``AGENT_SESSION_SUMMARY_ENABLED`` is untouched (AC-5).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import orjson
from scripts.eval.fre994_digest_compression_curve.arms import (
    Arm,
    TokenParts,
    decompose_tokens,
    generation_model_key,
    system_prompt_for,
    tools_for,
)

from personal_agent.llm_client import ModelRole
from personal_agent.memory.session_digest import SessionDigest, digest_token_count
from personal_agent.memory.session_digest_wire import DIGEST_TOOL_NAME, DigestEnvelope, to_storage
from personal_agent.telemetry.trace import SystemTraceContext

if TYPE_CHECKING:  # pragma: no cover — typing only
    from personal_agent.captains_log.capture import TaskCapture

#: Stop reasons meaning "the ceiling cut this off", across provider vocabularies.
#: Mirrors the producer's own set.
TRUNCATION_FINISH_REASONS = frozenset({"length", "max_tokens"})

#: Cap on a recorded error string, mirroring the producer's failure-detail cap. A parse
#: error can embed the offending value, and the digest is built from real conversation —
#: so an uncapped error field is a channel for writing session text to disk.
MAX_ERROR_CHARS = 500

#: Consecutive failures that mean the harness is broken rather than the model failing.
#: FRE-996's first attempt produced 90 uniform `provider_error` rows from an unregistered
#: cost gate, which reads like a result rather than a broken setup.
ABORT_AFTER_CONSECUTIVE_ERRORS = 3


@dataclass(frozen=True)
class GenerationRecord:
    """One (session × arm) cell, fully classified.

    Attributes:
        session_id: The session.
        arm: The arm name.
        outcome: Exactly one of :data:`OUTCOMES` — mutually exclusive and exhaustive, so
            the counts add to the call count. A classifier with an implicit "everything
            else" bucket is how a truncated reply gets quietly scored as clean.
        rendered_tokens: Consumer-facing token count, or None when unusable.
        within_bound: Whether ``rendered_tokens`` fits the arm's stated maximum.
        content_bearing: Whether any slot carried an item.
        truncated: Whether the reply was cut off at the call ceiling.
        prompt_tokens: Billed input.
        completion_tokens: Billed output.
        content_tokens: Model-authored value-string tokens (§4.4).
        structural_tokens: Billed output minus content (§4.4).
        cost_usd: Billed cost for the call.
        finish_reason: The provider's stop reason, left intact by the explicit-tool path.
        digest: The parsed digest, for the judge. None when unusable.
        error: Capped diagnostic, or None.
    """

    session_id: str
    arm: str
    outcome: str
    rendered_tokens: int | None
    within_bound: bool
    content_bearing: bool
    truncated: bool
    prompt_tokens: int | None
    completion_tokens: int | None
    content_tokens: int | None
    structural_tokens: int | None
    cost_usd: float
    finish_reason: str | None
    digest: SessionDigest | None
    error: str | None


#: Every outcome a reply can be assigned.
OUTCOMES = (
    "ok",
    "ok_at_ceiling",
    "truncated",
    "invalid_json",
    "contract_drift",
    "empty",
    "provider_error",
)


async def generate(arm: Arm, *, prompt: str, session_id: str) -> dict[str, Any]:
    """Dispatch one generation call on one arm.

    Mirrors the producer's dispatch minus the retry loop and the settings gate — the
    production model, the production prompt under this arm's length policy, and the
    production contract.

    Args:
        arm: The arm being run.
        prompt: The assembled transcript.
        session_id: For trace correlation.

    Returns:
        The raw reply.
    """
    from personal_agent.llm_client.factory import get_llm_client_for_key  # noqa: PLC0415

    tools, tool_choice = tools_for(arm)
    # Billed to `study`, never `captains_log`: FRE-839's one-off-corpus lane, so this run
    # cannot contend with the live digest cap nor pollute the cost series the audit
    # measures the digest's real spend from. `on_denial: raise` makes a denial a loud,
    # resumable stop rather than a silently thinned sample.
    client = get_llm_client_for_key(generation_model_key(), budget_role="study")
    return await client.respond(
        role=ModelRole.PRIMARY,
        messages=[{"role": "user", "content": prompt}],
        system_prompt=system_prompt_for(arm),
        tools=tools,
        tool_choice=tool_choice,
        max_tokens=arm.call_ceiling,
        # Temperature is deliberately NOT pinned, because it cannot be: claude-sonnet-5
        # rejects any value but 1 (litellm UnsupportedParamsError). The arms therefore
        # carry sampling variance no harness discipline removes, and two arms on one
        # session are not deterministic counterfactuals — stated as a limitation rather
        # than quietly assumed away.
        trace_ctx=SystemTraceContext.new("fre994_curve", session_id=session_id),
    )


def _payload(response: dict[str, Any]) -> str:
    """The digest JSON, from the tool call where the contract puts it."""
    for call in response.get("tool_calls") or []:
        if call.get("name") == DIGEST_TOOL_NAME and call.get("arguments"):
            return str(call["arguments"])
    # The contract is forced, so free text means the model escaped it. Returned rather
    # than discarded so the outcome is classified rather than counted as empty.
    return response.get("content", "") or ""


def classify(
    response: dict[str, Any],
    *,
    arm: Arm,
    session_id: str,
    ended_at: datetime,
    captures: Sequence[TaskCapture],
) -> GenerationRecord:
    """Assign exactly one outcome and every delivery measurement.

    Args:
        response: The raw reply.
        arm: The arm it was generated on.
        session_id: The session.
        ended_at: The session's last-turn timestamp, stamped onto unresolved items by
            the producer-owned half of the contract.
        captures: The session's captures, which correction spans are quoted from —
            also producer-owned (FRE-1024).

    Returns:
        The classified record.
    """
    payload = _payload(response)
    usage = response.get("usage") or {}
    completion_tokens = usage.get("completion_tokens")
    at_ceiling = response.get("finish_reason") in TRUNCATION_FINISH_REASONS or (
        isinstance(completion_tokens, int) and completion_tokens >= arm.call_ceiling
    )

    # `None` rather than 0 when the provider reported no usage: substituting 0 makes
    # structural_tokens = -content_tokens, and a large negative row entering the p95 pulls
    # the recommended call ceiling DOWN — the direction that causes truncation. A missing
    # measurement is reported as missing, exactly as a provider_error row is.
    parts = (
        decompose_tokens(payload, output_tokens=completion_tokens)
        if isinstance(completion_tokens, int)
        else TokenParts(content_tokens=None, structural_tokens=None, unusable=True)
    )
    base: dict[str, Any] = {
        "session_id": session_id,
        "arm": arm.name,
        "rendered_tokens": None,
        "within_bound": False,
        "content_bearing": False,
        "truncated": at_ceiling,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": completion_tokens,
        "content_tokens": parts.content_tokens,
        "structural_tokens": parts.structural_tokens,
        "cost_usd": float(response.get("cost_usd") or 0.0),
        "finish_reason": response.get("finish_reason"),
        "digest": None,
        "error": None,
    }

    if not payload.strip():
        # Ceiling first: a generation that exhausted its budget before emitting anything
        # usable is a truncation, not a model that chose to say nothing. Testing `empty`
        # ahead of `at_ceiling` mislabels it and understates truncation — which it did on
        # FRE-996's own first pass.
        return GenerationRecord(outcome="truncated" if at_ceiling else "empty", **base)

    try:
        envelope = DigestEnvelope.model_validate(orjson.loads(payload))
        _, digest = to_storage(envelope, ended_at=ended_at, captures=captures)
    except orjson.JSONDecodeError as e:
        base["error"] = str(e)[:MAX_ERROR_CHARS]
        return GenerationRecord(outcome="truncated" if at_ceiling else "invalid_json", **base)
    except Exception as e:  # noqa: BLE001 — any contract failure is a measured class
        base["error"] = f"{type(e).__name__}: {e}"[:MAX_ERROR_CHARS]
        return GenerationRecord(outcome="truncated" if at_ceiling else "contract_drift", **base)

    rendered = digest_token_count(digest)
    content_bearing = bool(
        digest.established or digest.decisions or digest.unresolved or digest.corrections
    )
    base |= {
        "rendered_tokens": rendered,
        # The unbounded arm has no stated maximum, so nothing can fall outside it.
        "within_bound": True if arm.unbounded else rendered <= arm.max_tokens,
        "content_bearing": content_bearing,
        "digest": digest,
    }
    if not content_bearing:
        # Parsed cleanly, filled no slot. ADR-0124 allows an empty digest, but in
        # production it returns GENERATED and so marks the session clean forever — the
        # failure most hostile to the consumer, and the one FRE-996 flagged for watching.
        return GenerationRecord(outcome="empty", **base)

    # A digest cut off mid-list still parses as a valid, shorter digest. Scoring that as
    # clean is the cheapest way this measurement produces a false success, so it gets its
    # own class rather than being folded into `ok`.
    return GenerationRecord(outcome="ok_at_ceiling" if at_ceiling else "ok", **base)


def record_to_json(record: GenerationRecord) -> dict[str, Any]:
    """Serialise a record for the run's JSONL, keeping the digest readable.

    Args:
        record: The record.

    Returns:
        A JSON-safe dict.
    """
    payload = {k: v for k, v in record.__dict__.items() if k != "digest"}
    payload["digest"] = record.digest.model_dump(mode="json") if record.digest else None
    return payload
