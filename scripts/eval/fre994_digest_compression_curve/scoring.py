"""Ground truth, coverage judging and the validity gates (FRE-994 §4.3).

The loss endpoint asks whether a digest dropped a **consequential conclusion**, which is
ADR-0124 D3's own definition of wrong. That needs two things the generation stage cannot
supply: a list of what a session actually concluded, and a verdict on whether a given
digest carries each one.

Both are model calls, and both are on ``gpt-5.4-mini`` — a different family from the
Sonnet generator, deliberately. One model writing the reference set and then grading
against it shares its blind spots between the ground truth and its own scorer, and
nothing in the design would notice.

**What the gates in this module can and cannot establish.** They compare the extractor
and the judge against a reference set one person wrote by hand. Passing means two models
reproduced that person's reading. It does not mean the reading is right, and the person
who wrote it also designed the arms and knows the thresholds. The hand references are
committed under ``references/`` so the judgement can be audited rather than trusted, and
the write-up states the limitation in its own words.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scripts.eval.fre994_digest_compression_curve.arms import SCORING_MODEL_KEY

from personal_agent.llm_client import ModelRole
from personal_agent.memory.session_digest import SessionDigest
from personal_agent.telemetry.trace import SystemTraceContext

#: Quoted verbatim into both prompts so the extractor and the judge apply ADR-0124 D3's
#: definition rather than an ad-hoc sense of importance — and so the hand-authored
#: reference set is written against the same words.
CONSEQUENTIAL_DEFINITION = (
    "A conclusion is CONSEQUENTIAL when a future reader who did not attend this session "
    "would repeat settled work without it, or would be misled about what was established, "
    "rejected, resolved or contradicted. Passing remarks, restatements of the question, "
    "pleasantries, and process narration are NOT consequential. Something the session left "
    "explicitly open IS consequential, because a reader who thinks it was settled is wrong."
)

_EXTRACTION_SYSTEM = f"""\
You are reading a complete transcript of one working session between a user and an \
assistant. List the consequential conclusions the session reached.

{CONSEQUENTIAL_DEFINITION}

Rules:
- One conclusion per item, stated so it stands alone without the transcript.
- Ground every item in what the transcript actually says. Do not infer what probably \
happened next, and do not add advice.
- Most sessions have between two and eight. A long session is not automatically a rich one.
- If the session reached none, return an empty list. That is a legitimate answer.
"""

_JUDGE_SYSTEM = f"""\
You are checking whether a session digest carries a specific conclusion the session \
reached. You are given one reference conclusion and the full digest.

{CONSEQUENTIAL_DEFINITION}

Answer with exactly one verdict for the reference conclusion:
- "covered": the digest states this conclusion, or states something a reader would draw \
it from. Different wording is fine; the meaning must survive.
- "partial": the digest gestures at the topic but a reader could not rely on the \
conclusion — the specific finding, decision or open question is missing or blurred.
- "missing": the digest does not carry it at all.

Judge the meaning, not the phrasing, and never reward length. A digest that says more \
about other things does not thereby cover this one.
"""

#: Structured-output schema for the extractor.
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conclusions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "why_consequential": {"type": "string"},
                },
                "required": ["text", "why_consequential"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["conclusions"],
    "additionalProperties": False,
}

#: Structured-output schema for one coverage verdict.
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["covered", "partial", "missing"]},
        "evidence": {"type": "string"},
    },
    "required": ["verdict", "evidence"],
    "additionalProperties": False,
}

#: Verdicts that count as carrying the conclusion in the primary endpoint. ``partial``
#: scores 0 here — an item half-carried is an item a future reader cannot rely on — and
#: 0.5 in the reported sensitivity analysis. Both are precommitted (§4.2).
COVERED_VERDICTS = frozenset({"covered"})
COVERED_VERDICTS_LENIENT = {"covered": 1.0, "partial": 0.5, "missing": 0.0}


def _response_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Structured-output payload for the scoring model.

    ``response_format`` rather than an explicit tool, unlike the generator: FRE-996
    rejected it there because litellm rewrites the provider's stop reason to ``"stop"``
    under ``json_mode``, destroying the truncation signal. Nothing here measures
    truncation — a scoring reply is two fields — so the simpler route is correct, and the
    scoring model is OpenAI-family where ``response_format`` is native anyway.
    """
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "schema": schema, "strict": True},
    }


def _client() -> Any:
    from personal_agent.llm_client.factory import get_llm_client_for_key  # noqa: PLC0415

    # Billed to `study`, the one-off-corpus lane (FRE-839), never to `captains_log`:
    # this run must not contend with the live digest cap nor pollute the cost series the
    # audit measures the digest's real spend from. `on_denial: raise` makes a budget
    # denial a loud stop rather than a silently thinned sample.
    return get_llm_client_for_key(SCORING_MODEL_KEY, budget_role="study")


async def extract_conclusions(transcript: str, *, session_id: str) -> dict[str, Any]:
    """Ask the extractor for the session's consequential conclusions.

    Args:
        transcript: The assembled session transcript, as the generator sees it.
        session_id: For trace correlation.

    Returns:
        The raw reply, carrying ``content`` and usage.
    """
    return await _client().respond(
        role=ModelRole.PRIMARY,
        messages=[{"role": "user", "content": transcript}],
        system_prompt=_EXTRACTION_SYSTEM,
        response_format=_response_format("consequential_conclusions", EXTRACTION_SCHEMA),
        max_tokens=2_048,
        trace_ctx=SystemTraceContext.new("fre994_extract", session_id=session_id),
    )


async def judge_coverage(
    *, conclusion: str, digest: SessionDigest, session_id: str
) -> dict[str, Any]:
    """Ask the judge whether one digest carries one reference conclusion.

    One call per (conclusion, digest) rather than one call listing every conclusion:
    a single call invites the model to spread a global impression of digest quality
    across all its verdicts, which is exactly the halo the primary endpoint must not
    inherit.

    Args:
        conclusion: One reference conclusion.
        digest: The digest under test.
        session_id: For trace correlation.

    Returns:
        The raw reply, carrying ``content`` and usage.
    """
    rendered = render_digest(digest)
    prompt = f"REFERENCE CONCLUSION:\n{conclusion}\n\nDIGEST:\n{rendered}"
    return await _client().respond(
        role=ModelRole.PRIMARY,
        messages=[{"role": "user", "content": prompt}],
        system_prompt=_JUDGE_SYSTEM,
        response_format=_response_format("coverage_verdict", VERDICT_SCHEMA),
        max_tokens=512,
        trace_ctx=SystemTraceContext.new("fre994_judge", session_id=session_id),
    )


def render_digest(digest: SessionDigest) -> str:
    """Render a digest the way a reader would receive it.

    The judge must see what a consumer sees, not the storage JSON: judging the wire
    payload would let key names and basis tags stand in for content the reader never
    reads.

    Args:
        digest: The digest to render.

    Returns:
        A plain-text rendering, or a marker when every slot is empty.
    """
    lines: list[str] = []
    for slot in ("established", "decisions", "unresolved"):
        items = getattr(digest, slot)
        if items:
            lines.append(f"{slot.upper()}:")
            lines.extend(f"- {item.text}" for item in items)
    if digest.corrections:
        lines.append("CORRECTIONS:")
        lines.extend(f"- {c.text}" for c in digest.corrections)
    return "\n".join(lines) if lines else "(the digest is empty — it carries no items)"


# ── Validity gates (§4.3) ───────────────────────────────────────────────────


@dataclass(frozen=True)
class AgreementResult:
    """One gate's measurement against its precommitted threshold.

    Attributes:
        name: Gate identifier, as it appears in the write-up.
        value: The measured statistic.
        threshold: The value precommitted before the run.
        passed: Whether the gate holds.
        detail: Counts behind the statistic, so a failure is diagnosable.
    """

    name: str
    value: float
    threshold: float
    passed: bool
    detail: dict[str, Any]


#: Precommitted before any spend (§4.3). Named here rather than inline so a threshold
#: cannot be edited into agreement with a result without the diff showing it.
EXTRACTOR_RECALL_MIN = 0.80
EXTRACTOR_SPURIOUS_MAX = 0.20
JUDGE_AGREEMENT_MIN = 0.80
JUDGE_KAPPA_MIN = 0.60
ANCHOR_EMPTY_MAX = 0.05
ANCHOR_SELF_MIN = 0.95


def extractor_recall(*, matched: int, reference_total: int) -> AgreementResult:
    """Fraction of hand-authored reference items the extractor also found.

    Args:
        matched: Reference items the extractor recovered.
        reference_total: Hand-authored reference items.

    Returns:
        The gate result.
    """
    value = matched / reference_total if reference_total else 0.0
    return AgreementResult(
        name="extractor_recall",
        value=value,
        threshold=EXTRACTOR_RECALL_MIN,
        passed=value >= EXTRACTOR_RECALL_MIN,
        detail={"matched": matched, "reference_total": reference_total},
    )


def extractor_spurious_rate(*, spurious: int, extracted_total: int) -> AgreementResult:
    """Fraction of extracted items the author judges not consequential.

    Args:
        spurious: Extracted items judged not consequential.
        extracted_total: Items the extractor returned.

    Returns:
        The gate result.
    """
    value = spurious / extracted_total if extracted_total else 0.0
    return AgreementResult(
        name="extractor_spurious_rate",
        value=value,
        threshold=EXTRACTOR_SPURIOUS_MAX,
        passed=value <= EXTRACTOR_SPURIOUS_MAX,
        detail={"spurious": spurious, "extracted_total": extracted_total},
    )


def judge_agreement(pairs: list[tuple[str, str]]) -> tuple[AgreementResult, AgreementResult]:
    """Raw agreement and Cohen's κ between the judge and the author.

    κ is reported alongside raw agreement because raw agreement is inflated whenever one
    verdict dominates — and ``covered`` will dominate. A judge that answered "covered"
    unconditionally would score highly on agreement and zero on κ.

    Args:
        pairs: ``(author_verdict, judge_verdict)`` over the same items.

    Returns:
        The raw-agreement gate and the κ gate.
    """
    n = len(pairs)
    observed = sum(1 for a, b in pairs if a == b) / n if n else 0.0

    labels = sorted({v for pair in pairs for v in pair})
    expected = 0.0
    for label in labels:
        p_author = sum(1 for a, _ in pairs if a == label) / n if n else 0.0
        p_judge = sum(1 for _, b in pairs if b == label) / n if n else 0.0
        expected += p_author * p_judge
    # κ is undefined when the raters are in perfect agreement AND used one label only:
    # there is no chance-agreement baseline to correct against. Reported as 1.0 with the
    # degenerate case named in `detail`, never silently as 0.
    kappa = 1.0 if expected >= 1.0 else (observed - expected) / (1.0 - expected)

    detail = {"n": n, "labels": labels, "expected_agreement": round(expected, 4)}
    return (
        AgreementResult(
            name="judge_agreement",
            value=observed,
            threshold=JUDGE_AGREEMENT_MIN,
            passed=observed >= JUDGE_AGREEMENT_MIN,
            detail=detail,
        ),
        AgreementResult(
            name="judge_kappa",
            value=kappa,
            threshold=JUDGE_KAPPA_MIN,
            passed=kappa >= JUDGE_KAPPA_MIN,
            detail=detail | {"degenerate_single_label": expected >= 1.0},
        ),
    )


def anchor_results(*, empty_retention: float, self_retention: float) -> list[AgreementResult]:
    """The two plumbing anchors.

    These are **not** validity evidence and are labelled as such in the write-up. A
    scorer can score an empty digest at zero and a reference against itself at one while
    still misjudging compressed paraphrases — which are the cases that select the arm.
    They catch a broken scorer, nothing more.

    Args:
        empty_retention: Retention scored for an empty digest.
        self_retention: Retention scored for the reference against itself.

    Returns:
        Both anchor results.
    """
    return [
        AgreementResult(
            name="anchor_empty",
            value=empty_retention,
            threshold=ANCHOR_EMPTY_MAX,
            passed=empty_retention <= ANCHOR_EMPTY_MAX,
            detail={"direction": "must be at or below"},
        ),
        AgreementResult(
            name="anchor_self",
            value=self_retention,
            threshold=ANCHOR_SELF_MIN,
            passed=self_retention >= ANCHOR_SELF_MIN,
            detail={"direction": "must be at or above"},
        ),
    ]
