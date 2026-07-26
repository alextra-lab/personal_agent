"""Arms, token decomposition and cost projection (FRE-994 §4, §5.1).

**The knob is the prompt's stated token policy, because it is the only lever that
works.** Rev 2 of this plan parameterised the arms structurally — items per slot by
tokens per item — on the reasoning that the digest's destination is a JSON blob on the
``Session`` node and so constrains *shape* rather than size. FRE-996 then measured that
directly and the reasoning does not survive it: per-slot item ceilings moved the rendered
median by three tokens (221 → 224), because item *text* is unbounded and a model
satisfies "at most five items" by writing five longer ones. The schema dialect has no
``maxLength`` (FRE-995 §8.2), so structure cannot express length at all.

What is left is the prompt's own LENGTH rule, and FRE-996's numbers suggest it is doing
real work: told 180 target / 250 maximum, the generator lands at a rendered median of
208–224. The tail is where it fails — p90 341–389, all-pass 413–419. So the curve's
question is whether moving that stated number moves the distribution, and at what point
moving it down starts costing consequential conclusions.

Item ceilings survive as a **separate arm answering a separate question**: FRE-996 §5.1
found the bounded variant produced content on 27 of 30 sessions against 25 and 24, and
flagged length and completion as questions that should not be conflated. That arm uses the
production :func:`digest_tool` with ``bounded=True``, not a re-implementation.

Every constant below that prices the run is **measured from FRE-996's committed records**
(``telemetry/evaluation/fre996-pilot-final.json``, 90 calls), not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import orjson

from personal_agent.config import load_model_config, resolve_role_model_key
from personal_agent.llm_client.token_counter import estimate_tokens
from personal_agent.memory.session_digest_wire import digest_tool, digest_tool_choice
from personal_agent.second_brain.session_summary import system_prompt

#: Catalog key of the scoring model. Deliberately a different family from the
#: generator: one model both writing the reference set and grading against it would
#: share its blind spots between the ground truth and its own scorer.
SCORING_MODEL_KEY = "gpt-5.4-mini"

#: Output ceiling for every generation call. Above production's 2,048 so the ceiling is
#: never the binding constraint — a curve measured against a wall measures the wall. The
#: highest contract output FRE-996 recorded was 1,050, so this binds nothing but the
#: unbounded arm, which is exactly the arm whose natural length is the question.
CALL_OUTPUT_CEILING = 4_096

#: ADR-0124 D3's own ratio between the target it states and the maximum it enforces
#: (180 / 250). Each arm moves both together at this ratio, so an arm is a **policy
#: pair**, not an isolated hard maximum — named as a confound, not averaged over.
TARGET_TO_MAX_RATIO = 0.72


@dataclass(frozen=True)
class Arm:
    """One point on the curve.

    Attributes:
        name: Stable identifier, used as the JSONL key and the table row.
        max_tokens: The maximum the prompt states. Zero on the unbounded arm.
        bounded_schema: Send the contract with per-slot item ceilings
            (:func:`digest_tool` ``bounded=True``). Answers FRE-996 §5.1's completion
            question, which is orthogonal to length.
        unbounded: When True the LENGTH paragraph leaves the prompt entirely rather
            than being widened — a large number is still an instruction, and this arm
            measures what the generator writes when nothing constrains it. It is the
            reference the loss endpoint is measured *against*, so it is not optional.
    """

    name: str
    max_tokens: int = 0
    bounded_schema: bool = False
    unbounded: bool = False

    @property
    def target_tokens(self) -> int:
        """The target the prompt states, derived at ADR-0124 D3's own ratio."""
        return int(self.max_tokens * TARGET_TO_MAX_RATIO)


#: The curve. ``t250`` is the policy deployed today, so it anchors the curve to the
#: incumbent rather than floating free of it; ``t400`` sits in the region FRE-996 §2.2
#: measured as where every content-bearing contract digest passes (413–419).
ARMS: tuple[Arm, ...] = (
    Arm("t120", 120),
    Arm("t180", 180),
    Arm("t250", 250),  # deployed today: 180 target / 250 maximum
    Arm("t400", 400),
    Arm("unbounded", unbounded=True),
    Arm("t250_bounded", 250, bounded_schema=True),
)

ARMS_BY_NAME = {a.name: a for a in ARMS}


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
    return system_prompt(target_tokens=arm.target_tokens, max_tokens=arm.max_tokens)


def tools_for(arm: Arm) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The contract this arm sends, and the choice that forces it.

    Always the production contract from ``session_digest_wire`` — FRE-996 shipped it and
    the producer sends it on every call, so a harness-local schema would calibrate a
    contract that is not deployed.

    Args:
        arm: The arm being run.

    Returns:
        The ``tools`` list and the ``tool_choice`` payload.
    """
    return [digest_tool(bounded=arm.bounded_schema)], digest_tool_choice()


# ── Token decomposition (§4.4) ──────────────────────────────────────────────

#: JSON keys whose values are model-authored prose. Everything else in the payload
#: is scaffolding the contract requires.
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
    undercounts Anthropic tokenisation by about half again (:data:`PROVIDER_TOKEN_RATIO`);
    the structural residual therefore skews high and is read as an upper bound on the
    envelope, not a point estimate. The comparison the study draws — content against the
    *bound*, both on the same estimator — is unaffected.

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


# ── Cost projection (AC-6) ──────────────────────────────────────────────────
#
# Every constant here is measured from FRE-996's 90 committed call records rather than
# assumed, because rev 2 of this plan priced the run on the cl100k estimator alone and
# understated billed input by roughly a third — the same class of error, a number that
# looks measured because it came out of code, that this ticket exists to correct.

#: Anthropic's billed ``prompt_tokens`` divided by this repo's cl100k estimate of the
#: same prompt. Measured over FRE-996's 30 sessions: p50 1.535, mean 1.544, range
#: 1.49–1.70. The estimator the cost gate reserves against is systematically low, so a
#: projection that skips this correction under-prices the run by a third.
PROVIDER_TOKEN_RATIO = 1.535

#: Billed input tokens the contract's tool definition adds to every call. Measured
#: exactly — FRE-996's arm B minus arm A prompt tokens was 1,663 on all 30 sessions,
#: with zero variance, because the definition is identical every time.
TOOL_DEFINITION_TOKENS = 1_663

#: Billed output tokens per rendered digest token, over FRE-996's 60 contract calls
#: (p50 2.4, mean 2.7). Most of a digest call's output is envelope — braces, keys and
#: basis tags — which is why a rendered-token bound and a call ceiling are different
#: numbers and must be stated separately (AC-3).
OUTPUT_ENVELOPE_PER_RENDERED_TOKEN = 2.7

#: Rendered tokens produced as a multiple of the maximum the prompt states, at the p90
#: of FRE-996's contract arms (341/250 = 1.36 and 389/250 = 1.56). Taken at the p90 and
#: not the median deliberately: this projection is the upper bound the owner authorises
#: spend against, and a median-priced run is one that overspends half the time.
RENDERED_OVERSHOOT_P90 = 1.5

#: Upper bound for the unbounded arm's output. No contract-mode measurement of an
#: unconstrained digest exists — FRE-996's free-text arm A ran to production's 2,048
#: ceiling on 5 of 30 calls — so the ceiling itself is the only honest bound to price.
UNBOUNDED_OUTPUT_UPPER_BOUND = 2_048


def projected_input_tokens(estimated_prompt_tokens: int) -> int:
    """Billed input for one generation call, corrected to what the provider bills.

    Args:
        estimated_prompt_tokens: cl100k estimate of transcript plus system prompt.

    Returns:
        Projected billed ``prompt_tokens``, including the tool definition.
    """
    return round(estimated_prompt_tokens * PROVIDER_TOKEN_RATIO) + TOOL_DEFINITION_TOKENS


def projected_output_tokens(arm: Arm) -> int:
    """Upper-bound billed output for one call on this arm.

    Args:
        arm: The arm being priced.

    Returns:
        Projected billed ``completion_tokens``, never above the call ceiling.
    """
    if arm.unbounded:
        return min(UNBOUNDED_OUTPUT_UPPER_BOUND, CALL_OUTPUT_CEILING)
    rendered = arm.max_tokens * RENDERED_OVERSHOOT_P90
    return min(round(rendered * OUTPUT_ENVELOPE_PER_RENDERED_TOKEN), CALL_OUTPUT_CEILING)


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
        generation_input_tokens: Total billed input tokens across generation calls.
        generation_output_tokens: Total billed output tokens across generation calls.
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
