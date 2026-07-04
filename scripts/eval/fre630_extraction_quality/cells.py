"""FRE-766 — the model × reasoning benchmark matrix + pure eval helpers.

The owner-specified 5-cell matrix (+ the mini@medium baseline) that the harness drives
through the *real* extractor via the ``ExtractionModelOverride`` DI seam — one cell per
model, so cells run concurrently with no shared-config mutation. Everything here is pure
(no I/O, no LLM) so it is unit-tested: the cost function, the smoke failure-classifier,
and the baseline-reuse compatibility gate.

Reasoning controls are model-appropriate (verify-don't-assume): the GPT-5 family exposes
a discrete effort ladder (low/medium/high/xhigh); ``None`` = the provider default
(medium for GPT-5). Claude uses adaptive thinking, so its cell leaves ``reasoning_effort``
None. Sampling is model-appropriate: mini keeps its FRE-758 temperature pin (0.0); the
full model and Claude run at their defaults (temperature None).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from scripts.eval.fre630_extraction_quality.gold import ALLOWED_ENTITY_TYPES_V2

from personal_agent.second_brain.entity_extraction import ExtractionModelOverride

#: The 10 controlled entity types (ADR-0109 V2, live since the FRE-771 prompt swap) — a
#: smoke result must honour these.
_VALID_TYPES = ALLOWED_ENTITY_TYPES_V2
_VALID_CLASSES = frozenset({"World", "Personal", "System"})


@dataclass(frozen=True)
class ExtractionModelCell:
    """One benchmark cell: a model at a reasoning/sampling setting, plus its rates.

    Attributes:
        name: Short cell id (names the output run and the report row).
        override: The DI override handed to the extractor for this cell.
        input_rate: USD per input token (for the authoritative usage×rate cost).
        output_rate: USD per output token.
    """

    name: str
    override: ExtractionModelOverride
    input_rate: float
    output_rate: float


def cost_usd(cell: ExtractionModelCell, prompt_tokens: int, completion_tokens: int) -> float:
    """Authoritative benchmark cost for one call from token usage × the cell's rates.

    Computed directly (not via ``litellm.completion_cost``) so the eval cost is
    robust to unregistered/mis-prefixed model keys in the standalone harness
    (codex P0.2).

    Args:
        cell: The cell whose rates apply.
        prompt_tokens: Input tokens billed.
        completion_tokens: Output tokens billed (includes reasoning tokens).

    Returns:
        The call's USD cost.
    """
    return prompt_tokens * cell.input_rate + completion_tokens * cell.output_rate


def classify_smoke(result: Mapping[str, Any], call_stats: Sequence[Mapping[str, Any]]) -> str:
    """Classify a single smoke extraction so a failure is a finding, not a quality-zero.

    The extractor swallows exceptions/parse-failures into an empty result, which would
    otherwise score as a legitimate (bad) quality number. This distinguishes (codex
    P1.4):

    * ``provider_rejection:<Error>`` — the API call itself raised (bad model id, an
      unsupported ``reasoning_effort`` value, auth) — recorded via ``call_stats`` error.
    * ``empty_fallback`` — the model responded but produced nothing usable (parse
      failure / empty / timeout).
    * ``schema_violation`` — structured output that breaks the controlled vocabulary
      (an off-vocab entity type/class) — a stronger model ignoring the contract.
    * ``ok`` — schema-valid, contract-honouring output.

    Args:
        result: The extractor's returned dict for the smoke case.
        call_stats: The per-call stats sink (last entry carries ``error_class``).

    Returns:
        One of the classification strings above.
    """
    if call_stats:
        error_class = call_stats[-1].get("error_class")
        if error_class:
            return f"provider_rejection:{error_class}"

    entities = list(result.get("entities") or [])
    stances = list(result.get("stances") or [])
    claims = list(result.get("claims") or [])
    if not entities and not stances and not claims:
        return "empty_fallback"

    for entity in entities:
        if entity.get("type") not in _VALID_TYPES:
            return "schema_violation"
        if entity.get("class") not in _VALID_CLASSES:
            return "schema_violation"
    return "ok"


#: Fields that must match for a prior baseline run to be reused as this run's mini@medium
#: row (codex P1.3) — otherwise the numbers are not comparable and mini@medium is re-run.
_BASELINE_COMPAT_KEYS = (
    "gold_schema_version",
    "matcher_version",
    "prompt_hash",
    "samples",
    "fuzzy_threshold",
    "gold_set",
)


def baseline_compatible(baseline_meta: Mapping[str, Any], current_meta: Mapping[str, Any]) -> bool:
    """Return True iff a prior baseline run is comparable to this environment.

    Reusing the FRE-759 flag-OFF run as the mini@medium row is only valid when the
    gold schema, matcher, effective prompt hash, and sampling all match; any drift
    means re-run mini@medium in-environment instead of reusing stale numbers.

    Args:
        baseline_meta: The stored baseline run's ``meta`` block.
        current_meta: The metadata this environment would stamp for mini@medium.

    Returns:
        True when every compatibility key is equal.
    """
    return all(baseline_meta.get(k) == current_meta.get(k) for k in _BASELINE_COMPAT_KEYS)


# ── The matrix (owner-specified) ────────────────────────────────────────────────
_MINI_IN, _MINI_OUT = 7.5e-07, 4.5e-06  # gpt-5.4-mini $0.75/$4.50 per MTok
_FULL_IN, _FULL_OUT = 2.5e-06, 1.5e-05  # gpt-5.4 (full) $2.50/$15 per MTok
_SONNET_IN, _SONNET_OUT = 3e-06, 1.5e-05  # claude-sonnet-5 $3/$15 per MTok

#: mini@NONE — the baseline row = the CURRENT PROD extractor. MEASURED 2026-07-03:
#: gpt-5.4-mini with reasoning_effort unset produces ZERO reasoning tokens (identical to
#: explicit 'none'), so prod does no reasoning — this is @none, NOT @medium (the ticket's
#: "medium (default)" premise was wrong: the gpt-5.4 default is 'none'). temperature 0.0 =
#: the FRE-758 pin (allowed at 'none'). Matches the FRE-759 flag-OFF baseline; reused if
#: compatible, else re-run in-environment.
BASELINE_CELL = ExtractionModelCell(
    name="mini-none",
    override=ExtractionModelOverride(
        model_id="gpt-5.4-mini", provider="openai", reasoning_effort=None, temperature=0.0
    ),
    input_rate=_MINI_IN,
    output_rate=_MINI_OUT,
)

#: The 5 measured cells (the 'medium' rungs are set EXPLICITLY — unset == none on gpt-5.4;
#: mini-xhigh cut as a measured-non-viable finding).
CELLS: tuple[ExtractionModelCell, ...] = (
    # FRE-766 smoke findings baked in:
    #  * gpt-5 rejects temperature=0.0 once reasoning_effort is set (only temp=1 with
    #    reasoning; temp≠1 needs reasoning='none') → the reasoning cells run temperature None.
    #  * reasoning tokens count against max_tokens → the reasoning cells get 16000 headroom
    #    (mini@xhigh exhausted 8192 on reasoning and emitted no JSON).
    #  * 'medium' is set EXPLICITLY (unset defaults to 'none' on gpt-5.4, measured) so the
    #    medium rung is actually exercised.
    ExtractionModelCell(
        name="mini-medium",
        override=ExtractionModelOverride(
            model_id="gpt-5.4-mini",
            provider="openai",
            reasoning_effort="medium",
            temperature=None,
            max_tokens=16000,
        ),
        input_rate=_MINI_IN,
        output_rate=_MINI_OUT,
    ),
    ExtractionModelCell(
        name="mini-high",
        override=ExtractionModelOverride(
            model_id="gpt-5.4-mini",
            provider="openai",
            reasoning_effort="high",
            temperature=None,
            max_tokens=16000,
        ),
        input_rate=_MINI_IN,
        output_rate=_MINI_OUT,
    ),
    # mini-xhigh CUT (owner, 2026-07-03): measured non-viable for extraction — xhigh
    # reasoning is a runaway (32000 reasoning tokens = the whole budget, 0 JSON emitted,
    # ~210 s/call). Recorded as a finding; not a benchmark cell.
    ExtractionModelCell(
        name="full-medium",
        override=ExtractionModelOverride(
            model_id="gpt-5.4",
            provider="openai",
            reasoning_effort="medium",  # explicit — unset would be 'none' on gpt-5.4
            temperature=None,
            max_tokens=16000,
        ),
        input_rate=_FULL_IN,
        output_rate=_FULL_OUT,
    ),
    ExtractionModelCell(
        name="full-high",
        override=ExtractionModelOverride(
            model_id="gpt-5.4",
            provider="openai",
            reasoning_effort="high",
            temperature=None,
            max_tokens=16000,
        ),
        input_rate=_FULL_IN,
        output_rate=_FULL_OUT,
    ),
    ExtractionModelCell(
        name="sonnet5-adaptive",
        override=ExtractionModelOverride(
            model_id="claude-sonnet-5",
            provider="anthropic",
            reasoning_effort=None,  # Claude uses adaptive thinking, not the effort hint
            temperature=None,
        ),
        input_rate=_SONNET_IN,
        output_rate=_SONNET_OUT,
    ),
)

CELLS_BY_NAME: dict[str, ExtractionModelCell] = {c.name: c for c in (BASELINE_CELL, *CELLS)}
