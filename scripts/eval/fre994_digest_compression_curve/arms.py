"""Arms, response schema, token decomposition and cost projection (FRE-994 §4, §5.1).

**Arms are structural, not a global token budget.** The digest's destination is a
JSON-string property on the ``Session`` node (``memory/service.py`` —
``SET s.session_digest = orjson.dumps(...)``), read back whole and rendered; nothing
queries inside it. So the graph imposes no token bound at all — it imposes a *shape*.
A global "250 rendered tokens" is a read-time context-window constraint levied on
behalf of a Phase-2 consumer that does not exist yet, and it is not expressible in a
response schema. Items-per-slot and tokens-per-item are, and they are what both the
schema and the stored record actually constrain. Rendered tokens are still measured
on every arm — a future consumer pays them, and AC-3's call ceiling derives from
them — they simply stop being the knob.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import orjson
from pydantic import BaseModel, Field

from personal_agent.config import load_model_config, resolve_role_model_key
from personal_agent.llm_client.token_counter import estimate_tokens
from personal_agent.memory.session_digest import Correction, DigestItem
from personal_agent.second_brain.session_summary import system_prompt

#: Catalog key of the scoring model. Deliberately a different family from the
#: generator: one model both writing the reference set and grading against it would
#: share its blind spots between the ground truth and its own scorer.
SCORING_MODEL_KEY = "gpt-5.4-mini"

#: Output ceiling for every generation call. Set high enough that it is never the
#: binding constraint at the bounds under test — the production producer's 2,048 is
#: the wall 57% of live calls hit, and a curve run against that wall would measure
#: the wall rather than the bound.
CALL_OUTPUT_CEILING = 4_096


@dataclass(frozen=True)
class Arm:
    """One point on the curve.

    Attributes:
        name: Stable identifier, used as the JSONL key and the table row.
        max_items_per_slot: Structural ceiling on items in any one slot.
        max_tokens_per_item: Structural ceiling on the length of one item.
        implied_rendered_ceiling: Roughly what the pair permits once rendered.
            Reported for comparability with ADR-0124 D3's existing figure; never
            enforced, because enforcing it would reintroduce the instrument this
            study is testing.
        unbounded: When True the length rule leaves the prompt entirely rather than
            being widened — a large number is still an instruction, and this arm
            measures what the generator writes when nothing constrains it.
        structured: False reruns the arm in today's free-text-JSON mode, so the
            amendment can tell FRE-993 whether structured output alone removes the
            truncation class that produces its schema-invalid failures.
    """

    name: str
    max_items_per_slot: int = 0
    max_tokens_per_item: int = 0
    implied_rendered_ceiling: int = 0
    unbounded: bool = False
    structured: bool = True


ARMS: tuple[Arm, ...] = (
    Arm("s1x25", 1, 25, 100),
    Arm("s2x30", 2, 30, 240),  # ≈ ADR-0124 D3's 250 as deployed today
    Arm("s3x35", 3, 35, 420),
    Arm("s4x45", 4, 45, 720),
    Arm("s6x55", 6, 55, 1_320),
    Arm("unbounded", unbounded=True),
)

#: The mode contrast (§4.0): one arm rerun exactly as production emits today.
FREE_TEXT_CONTRAST_ARM = Arm("s2x30_freetext", 2, 30, 240, structured=False)


def system_prompt_for(arm: Arm) -> str:
    """Render the production system prompt under this arm's length policy.

    The prompt is imported from the producer, never copied: a curve run against a
    copy calibrates a prompt that is not deployed and drifts on the next edit.

    Args:
        arm: The arm being run.

    Returns:
        The rendered system prompt.
    """
    if arm.unbounded:
        return system_prompt(include_length_rule=False)
    return system_prompt(
        target_tokens=int(arm.implied_rendered_ceiling * 0.72),
        max_tokens=arm.implied_rendered_ceiling,
        max_items_per_slot=arm.max_items_per_slot,
        max_tokens_per_item=arm.max_tokens_per_item,
    )


# ── Response schema, derived from the stored record ─────────────────────────


class ResponseDigest(BaseModel):
    """The four slots exactly as the graph stores them.

    ``unresolved`` items are :class:`DigestItem`, not ``UnresolvedItem``: ``as_of``
    is stamped by the producer from the session's own ``ended_at`` (ADR-0124 D3 —
    compute state, generate meaning), so asking the model for it would invite the
    hallucinated timestamp the design excludes.
    """

    established: list[DigestItem] = Field(default_factory=list)
    decisions: list[DigestItem] = Field(default_factory=list)
    unresolved: list[DigestItem] = Field(default_factory=list)
    corrections: list[Correction] = Field(default_factory=list)


class DigestResponse(BaseModel):
    """What one generation call must return."""

    label: str
    digest: ResponseDigest


def _strip_annotations(node: object) -> object:
    """Remove ``title`` and ``description`` from a generated schema.

    Pydantic lifts each model's docstring into ``description``. Left in, every
    generation call would ship this module's internal rationale to the provider as
    part of the prompt — paid for on every call, and an instruction the design never
    intended the model to read.
    """
    if isinstance(node, dict):
        return {
            k: _strip_annotations(v) for k, v in node.items() if k not in ("title", "description")
        }
    if isinstance(node, list):
        return [_strip_annotations(v) for v in node]
    return node


def digest_response_schema() -> dict[str, Any]:
    """The JSON schema enforced on generation calls.

    Derived from the models the graph stores rather than hand-written, so it cannot
    drift from the record it is meant to produce.

    Returns:
        A JSON schema for :class:`DigestResponse`, stripped of generated prose.
    """
    stripped = _strip_annotations(DigestResponse.model_json_schema())
    assert isinstance(stripped, dict)
    return stripped


def response_format() -> dict[str, Any]:
    """The ``response_format`` payload for a structured generation call."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "session_digest",
            "schema": digest_response_schema(),
            "strict": False,
        },
    }


# ── Token decomposition (§4.4) ──────────────────────────────────────────────

#: JSON keys whose values are model-authored prose. Everything else in the payload
#: is scaffolding the schema requires.
_CONTENT_KEYS = frozenset({"text", "span", "evidence_span", "label"})


@dataclass(frozen=True)
class TokenParts:
    """Billed output split into what it said and what it cost to say it structurally.

    ``output_tokens / rendered_tokens`` cannot answer FRE-993's question, because it
    conflates two different causes of a 1,338-token call against a 250-token bound.
    This split tells them apart: content near the bound with output far above it is
    **envelope overhead** (the fix is a larger call ceiling); content far above the
    bound is **instruction-following failure** (the fix is a different bound or a
    different prompt).

    Attributes:
        content_tokens: Tokens in the payload's model-authored value strings. None
            when the payload could not be parsed.
        structural_tokens: Billed output minus content. None when unparsable.
        unusable: The payload did not parse — truncated mid-structure, or invalid.
            Reported rather than dropped: excluding these rows biases every ratio
            toward successes, which is exactly how the live defect stayed invisible
            for fourteen days.
    """

    content_tokens: int | None
    structural_tokens: int | None
    unusable: bool


def _collect_content(node: object, out: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _CONTENT_KEYS and isinstance(value, str):
                out.append(value)
            else:
                _collect_content(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_content(item, out)


def decompose_tokens(raw: str, *, output_tokens: int) -> TokenParts:
    """Split one call's billed output into content and structural tokens.

    The content count uses the same cl100k estimator the budget path uses, which
    systematically undercounts Anthropic tokenisation; the structural residual
    therefore skews slightly high. The comparison the study draws — content against
    the *bound*, both on the same estimator — is unaffected.

    Args:
        raw: The model's raw output.
        output_tokens: Billed output tokens for the call.

    Returns:
        The decomposition, or an ``unusable`` marker when the payload will not parse.
    """
    try:
        parsed = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return TokenParts(content_tokens=None, structural_tokens=None, unusable=True)

    strings: list[str] = []
    _collect_content(parsed, strings)
    content = sum(estimate_tokens(s) for s in strings)
    return TokenParts(
        content_tokens=content,
        structural_tokens=output_tokens - content,
        unusable=False,
    )


#: Live prior for the unbounded arm: FRE-993 measured mean output of 1,338 tokens
#: across 446 production calls. The unbounded arm has no caps to construct a payload
#: from, so this stands in — flagged as an observation, not a projection.
OBSERVED_UNBOUNDED_OUTPUT_TOKENS = 1_338


def estimate_max_output_tokens(arm: Arm) -> int:
    """Billed output of a maximally-compliant response, measured offline.

    A projection resting on a guessed envelope factor is a guess dressed as a
    number. A payload that fills every slot to the arm's own caps is constructible
    here, serialised exactly as the schema requires, and counted — so the structural
    overhead in the estimate is measured rather than assumed. It is an **upper**
    bound: a compliant response cannot bill more, and most will bill much less.

    Args:
        arm: The arm to estimate.

    Returns:
        Estimated billed output tokens, never above :data:`CALL_OUTPUT_CEILING`.
    """
    if arm.unbounded:
        return min(OBSERVED_UNBOUNDED_OUTPUT_TOKENS, CALL_OUTPUT_CEILING)

    # ~4 characters per token under cl100k, the estimator the budget path uses.
    filler = "x" * (arm.max_tokens_per_item * 4)
    item = {"text": filler, "basis": "assistant_reasoning", "span": None, "locator": None}
    correction = {
        **item,
        "tier": "self_correction",
        "evidence_span": filler,
        "evidence_locator": {"capture_id": "0" * 36, "field": "assistant_text"},
    }
    payload = {
        "label": "x" * 90,
        "digest": {
            "established": [item] * arm.max_items_per_slot,
            "decisions": [item] * arm.max_items_per_slot,
            "unresolved": [item] * arm.max_items_per_slot,
            "corrections": [correction] * arm.max_items_per_slot,
        },
    }
    return min(estimate_tokens(orjson.dumps(payload).decode()), CALL_OUTPUT_CEILING)


# ── Cost projection (AC-6) ──────────────────────────────────────────────────


@dataclass(frozen=True)
class CostProjection:
    """Projected spend, priced per stage at that stage's own model."""

    generation_usd: float
    scoring_usd: float

    @property
    def total_usd(self) -> float:
        """Total projected spend."""
        return self.generation_usd + self.scoring_usd


def _rates(model_key: str) -> tuple[float, float]:
    """Input and output $/token for a catalog key.

    Read from the config-owned cost matrix rather than hardcoded — prices live in
    ``config/models.yaml`` and a second copy here would silently misstate the figure
    the owner authorises the run against.
    """
    model = load_model_config().models[model_key]
    return float(model.input_cost_per_token or 0.0), float(model.output_cost_per_token or 0.0)


def generation_model_key() -> str:
    """Catalog key the production producer resolves to for ``session_summary``."""
    return resolve_role_model_key("session_summary")


def project_cost(
    *,
    generation_input_tokens: int,
    generation_output_tokens: int,
    scoring_input_tokens: int,
    scoring_output_tokens: int,
) -> CostProjection:
    """Price a planned run.

    Args:
        generation_input_tokens: Total input tokens across generation calls.
        generation_output_tokens: Total output tokens across generation calls.
        scoring_input_tokens: Total input tokens across extraction and judging.
        scoring_output_tokens: Total output tokens across extraction and judging.

    Returns:
        The projection, split by stage.
    """
    gen_in, gen_out = _rates(generation_model_key())
    score_in, score_out = _rates(SCORING_MODEL_KEY)
    return CostProjection(
        generation_usd=generation_input_tokens * gen_in + generation_output_tokens * gen_out,
        scoring_usd=scoring_input_tokens * score_in + scoring_output_tokens * score_out,
    )
