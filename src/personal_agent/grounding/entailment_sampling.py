"""The sampled offline arm of D3(d) (ADR-0138 / ADR-0087, FRE-1286).

Containment is necessary everywhere and sufficient almost everywhere, and ADR-0138 records
what it cannot reach as accepted residual risk: *"a source that contains the asserted token
but does not support the claim. D3(c) cannot see this; only sampled D3(d) can, and only
after the fact."* A source saying *"not sold in France"* contains every token of *"sold in
France"*; *"some"* passes for *"all"*.

This module measures that residue. It **never** touches the turn — the ADR is explicit that
per-claim inline entailment was rejected for v1 (Option 5), and the measured miss rate from
here is the evidence for any future decision to promote it. Until then this is the
instrument, not the enforcement.

**The two arms are disjoint.** :func:`select_offline_samples` excludes spans whose
``entity_free_predicate`` flag says the inline arm already judged them. That flag exists on
:class:`~personal_agent.grounding.verification.SpanVerification` for this reason alone:
once inline entailment turns a supported escalation into ``PASSED``, nothing else
distinguishes it from a span that passed containment outright, and the two arms would
overlap — re-billing the expensive class and double-counting it in the rate.

**The emitted row is adjudicable, not just countable.** It carries the claim, the source
identifier and the excerpt the judge actually read, alongside the verdict. An eval program
handed only ``miss=true`` has inherited the judge's opinion rather than the evidence for
it, and could never re-score a disputed sample — which matters precisely because the judge
is itself a model with its own error rate (measured over a labelled corpus by
``scripts/eval/fre1286_entailment``).

**Ownership.** The arm belongs to the ADR-0087 measurement program. Remediation when the
miss rate moves is a ticket against the grounding contract project, not a silent
re-tuning here.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from typing import Any

import structlog

from personal_agent.grounding.entailment import (
    EntailmentJudge,
    EntailmentVerdict,
    select_excerpt,
)
from personal_agent.grounding.source_registry import SourceRegistry
from personal_agent.grounding.verification import SpanVerification, TurnVerification
from personal_agent.telemetry.trace import TraceContext

log = structlog.get_logger(__name__)

SAMPLE_EVENT = "grounding_entailment_sample"
"""The log event one scored sample lands as.

One event per sampled span, so the miss rate per model is an aggregation over these rows
rather than a number someone computed by hand (AC-4).
"""

_MISS_VERDICTS: frozenset[EntailmentVerdict] = frozenset(
    {EntailmentVerdict.NOT_SUPPORTED, EntailmentVerdict.CONTRADICTED}
)
"""Verdicts that count as residue.

``UNDECIDED`` is deliberately absent: a judge that could not answer is a fact about the
judge, and counting it as a miss would let a provider outage read as the answering model
getting worse.
"""

_RNG = random.Random()
"""The sampler's own generator, so seeding it in a test cannot perturb anything else."""


def select_offline_samples(
    verification: TurnVerification,
    *,
    rate: float,
    rng: random.Random | None = None,
) -> tuple[SpanVerification, ...]:
    """Draw the spans this turn contributes to the offline measurement.

    An **independent Bernoulli draw per eligible span**, not per turn and not stratified,
    so a turn carrying many assertions contributes proportionally and the miss rate
    computed downstream estimates the population's.

    Args:
        verification: What the inline checks decided.
        rate: The configured sampling rate, 0.0 to 1.0.
        rng: Generator, injectable so the draw's shape is testable.

    Returns:
        The sampled spans. Eligibility is: the span passed every inline gate, it carries a
        resolvable identifier, and it is **not** in the inline arm's entity-free class.
    """
    if rate <= 0.0:
        return ()
    generator = rng if rng is not None else _RNG
    return tuple(
        span
        for span in verification.spans
        if span.passed
        and span.identifier
        and not span.entity_free_predicate
        and generator.random() < rate
    )


async def score_offline_samples(
    samples: Sequence[SpanVerification],
    registry: SourceRegistry,
    judge: EntailmentJudge,
    *,
    answering_model: str,
    judge_model: str,
    max_excerpt_chars: int,
    trace_ctx: TraceContext | None = None,
    emit: Callable[..., None] | None = None,
) -> None:
    """Judge each sampled span and emit one row per verdict.

    Runs in a background task after the turn has been delivered, which is what "offline"
    means here: off the critical path. It is deliberately sequential — nothing is waiting
    on it, and a burst of concurrent judge calls would contend with the live turns that
    are.

    Args:
        samples: What :func:`select_offline_samples` drew.
        registry: The turn's registry, for resolving each span's source.
        judge: The entailment judge.
        answering_model: The model that produced the claims — the axis AC-4's miss rate is
            read along.
        judge_model: The model that judged them, so a judge re-binding is visible as one.
        max_excerpt_chars: Window handed to the judge and recorded on the row.
        trace_ctx: The turn's trace context, threaded into every judge call. Passing the
            **turn's** context rather than letting the judge mint a system one is what
            keeps the sampled spend attributable: a minted context carries no session id,
            and ``LiteLLMClient`` logs ``cost_record_missing_identity`` at ERROR and books
            the call without identity. Found by running the corpus harness, which mints
            one legitimately — it has no turn — and surfaced the omission here.
        emit: Where a row goes. Defaults to the structured log; injected in tests so the
            assertion is on the row's content rather than on log capture.

    Returns:
        None. **A failed sample is a lost sample, never a raised error**: there is no turn
        left to fail, and a retry would bias the measurement toward whatever conditions
        happen to succeed on a second attempt.
    """
    write = emit if emit is not None else log.info
    trace_id = trace_ctx.trace_id if trace_ctx is not None else None
    session_id = trace_ctx.session_id if trace_ctx is not None else None

    for span in samples:
        source = registry.resolve(span.identifier) if span.identifier else None
        if source is None:
            continue
        excerpt = select_excerpt(span.text, source.content, max_chars=max_excerpt_chars)
        try:
            judgement = await judge.judge(span.text, excerpt, trace_ctx=trace_ctx)
        except Exception as exc:
            log.warning(
                "entailment_sample_failed",
                trace_id=trace_id,
                session_id=session_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            continue

        fields: dict[str, Any] = {
            "trace_id": trace_id,
            "session_id": session_id,
            "answering_model": answering_model,
            "judge_model": judge_model,
            "verdict": judgement.verdict.value,
            "miss": judgement.verdict in _MISS_VERDICTS,
            "reason": judgement.reason,
            "claim": span.text,
            "identifier": span.identifier,
            "excerpt": excerpt,
        }
        write(SAMPLE_EVENT, **fields)


__all__ = [
    "SAMPLE_EVENT",
    "score_offline_samples",
    "select_offline_samples",
]
