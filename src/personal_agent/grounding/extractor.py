"""Layer 2 — the span classifier (ADR-0138 D1, FRE-1281).

D1 is explicit that this is a model, not a rule: "No regular expression partitions
'world-fact claim' from 'generation.' Classification is performed by an explicit **span
extractor** — a small model or a structured-output pass — and its quality is therefore a
bounded, measurable property of the system rather than an assumption."

This module is that pass. It is handed only the regions
:mod:`personal_agent.grounding.code_regions` could not vouch for, and its output is
tightened by :mod:`personal_agent.grounding.span_policy` before anyone acts on it.

**It tiles; it does not emit a sparse list.** Every character of every region it receives
must land in exactly one returned segment, labelled ``claim_exempt``,
``claim_non_exempt`` or ``not_a_claim``. A plan review is why: under a sparse contract, a
claim the model simply omitted produced no record at all, so "the default is deny" was
undercut by silence rather than by a decision. ``not_a_claim`` is a judgement the corpus
can score; an omission is a seam.

**Anchoring is sequential, and failure is default-deny.** The model returns quoted text,
not offsets — models are unreliable at arithmetic over character positions. Because the
tiling is ordered and exhaustive, each quote is searched from the end of the previous
one, which resolves repeated identical text unambiguously. That matters concretely: D1's
own overlap example repeats a package name, first as the user's words and then as the
model's recommendation, and bare-quote anchoring could not tell them apart. A quote that
cannot be anchored is dropped, and the coverage rule in layer 3 then turns the hole it
leaves into a non-exempt span.

One model call per turn, not one per region: the regions are numbered in a single prompt.
This runs inline on the turn path once FRE-1282 lands, so the call count is latency the
user pays.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

import structlog

from personal_agent.grounding.code_regions import Region, RegionKind, partition_output
from personal_agent.grounding.span_policy import apply_policy
from personal_agent.grounding.spans import (
    ExemptRegion,
    NonExemptReason,
    Span,
    SpanExtraction,
    SpanLabel,
)

log = structlog.get_logger(__name__)

SPAN_TOOL_NAME = "emit_spans"
"""The forced tool the classifier answers through.

A tool rather than ``response_format``, following the FRE-996 finding recorded in
``memory/session_digest_wire.py``: for the deployed Anthropic models litellm turns
``response_format`` into a synthetic forced tool *and* overwrites the provider's
``stop_reason`` with ``"stop"``, which would make a truncated reply indistinguishable
from a clean one. Truncation must stay visible — a cut-off tiling is a coverage gap, and
layer 3 needs to see it as one.
"""

MAX_OUTPUT_TOKENS = 4096
"""Ceiling for one classification reply.

Bounded rather than left to the deployment's own maximum: the cost gate reserves against
whatever ceiling is declared, so an unbounded value would reserve a turn's worth of
budget for a few hundred tokens of segments.
"""

_LABEL_BY_WIRE = {
    "claim_exempt": SpanLabel.CLAIM_EXEMPT,
    "claim_non_exempt": SpanLabel.CLAIM_NON_EXEMPT,
    "not_a_claim": SpanLabel.NOT_A_CLAIM,
}

_REGION_BY_WIRE = {region.value: region for region in ExemptRegion}

SYSTEM_PROMPT = """\
You partition an assistant's output into atomic claims and decide which of them require \
a citation.

THE RULE. The default is DENY. Outside the exempt regions listed below, any span making \
a claim about the world requires a citation. Do not ask whether a claim is well known or \
obviously true — ask only whether an exempt region covers it.

SEGMENTATION. A segment is ONE atomic proposition. Segments never overlap and never \
nest. "Paris is France's capital and has 2.1 million residents" is TWO segments. A \
relative clause carrying its own checkable proposition is its own segment. Do not split \
a single proposition across its own grammar.

COVERAGE. You must cover EVERY character of every region you are given. Text that \
asserts nothing — connectives, greetings, questions, offers to continue — is \
"not_a_claim". That is a decision, not a gap: use it rather than leaving text out.

LABELS.
- "claim_non_exempt": asserts something about the world, and no exempt region covers it.
- "claim_exempt": asserts something, but an exempt region covers it. Name the region.
- "not_a_claim": asserts nothing about the world.

EXEMPT REGIONS (the complete list — there are no others):
- "code": code the user is being offered to run.
- "derived_arithmetic": arithmetic whose every input is itself cited.
- "attributed_restatement": the user's own words repeated WITH attribution ("you asked \
about X"). Presenting the same content as your own recommendation is NOT restatement.
- "connective_evaluative": judgement over cited material that introduces no externally \
checkable predicate of its own. Comparatives and orderings over cited attributes qualify. \
"Well regarded", "safe", "popular", "recommended" and "reliable" do NOT — each is an \
externally checkable claim about the world, however evaluative it sounds.
- "system_record": claims about THIS turn's own execution — what was searched, what was \
retrieved, that nothing was found. Not about the world.

AMBIGUITY. If you cannot decide whether a segment is exempt evaluation or a checkable \
claim, use "ambiguous" as the region. Do not guess.

NOTE ON THE TEXT YOU RECEIVE. Regions may be fragments of code comments, string \
literals, or prose lifted out of a code block. A world-fact claim inside a string \
literal or a comment is still a claim: `print("Paris has 9 million residents")` asserts \
something about Paris. A comment that merely describes the code around it does not.

Quote each segment EXACTLY as it appears, in order. Answer only through the tool.\
"""


def span_tool() -> dict[str, Any]:
    """Build the forced-tool definition carrying the classification contract.

    Returns:
        An OpenAI-format tool definition, ready to pass as ``tools=[...]``.
    """
    return {
        "type": "function",
        "function": {
            "name": SPAN_TOOL_NAME,
            "description": (
                "Emit the atomic segments of each region, in order, tiling every "
                "character of every region."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "segments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "region": {
                                    "type": "integer",
                                    "description": "Index of the region this segment is in.",
                                },
                                "quote": {
                                    "type": "string",
                                    "description": "The segment's text, quoted exactly.",
                                },
                                "label": {
                                    "type": "string",
                                    "enum": sorted(_LABEL_BY_WIRE),
                                },
                                "exempt_region": {
                                    "type": "string",
                                    "enum": sorted(_REGION_BY_WIRE),
                                    "description": "Required when label is claim_exempt.",
                                },
                            },
                            "required": ["region", "quote", "label"],
                        },
                    }
                },
                "required": ["segments"],
            },
        },
    }


def span_tool_choice() -> dict[str, Any]:
    """Force the tool, so the classifier cannot answer in prose."""
    return {"type": "function", "function": {"name": SPAN_TOOL_NAME}}


@runtime_checkable
class SpanExtractor(Protocol):
    """What FRE-1282's verification pass will depend on.

    Stated as a Protocol so the corpus harness, the deterministic baselines and the model
    pass are interchangeable at the seam that matters — the thing being measured is
    "spans out", not "which model".
    """

    async def extract(self, output: str, *, user_message: str | None = None) -> SpanExtraction:
        """Partition and classify one model output."""
        ...


def build_prompt(regions: Sequence[Region], user_message: str | None) -> str:
    """Render the classifiable regions as a numbered prompt.

    Args:
        regions: Layer 1's partition; only ``CLASSIFY`` regions are rendered.
        user_message: The user's turn, needed to judge attribution. Restatement is exempt
            because of the attribution, and attribution is undecidable without it.

    Returns:
        The user-message body for one classification call.
    """
    parts: list[str] = []
    if user_message:
        parts.append(
            "The user's own words this turn, for judging attributed restatement:\n"
            f"<<<USER>>>\n{user_message}\n<<<END USER>>>\n"
        )
    parts.append("Regions to segment:\n")
    for index, region in enumerate(regions):
        parts.append(f"<<<REGION {index}>>>\n{region.text}\n<<<END REGION {index}>>>\n")
    return "\n".join(parts)


def _anchor_sequentially(
    region: Region, quotes: Sequence[tuple[str, SpanLabel, ExemptRegion | None]]
) -> tuple[list[Span], int]:
    """Resolve ordered quotes to offsets within one region.

    Args:
        region: The region the quotes came from.
        quotes: ``(quote, label, exempt_region)`` in the order the model returned them.

    Returns:
        ``(spans, unanchored_count)``. An unanchorable quote is skipped; layer 3's
        coverage rule turns the hole it leaves into a non-exempt span, so dropping it is
        fail-closed rather than lossy.
    """
    spans: list[Span] = []
    unanchored = 0
    cursor = 0
    for quote, label, exempt_region in quotes:
        stripped = quote.strip()
        if not stripped:
            continue
        found = region.text.find(stripped, cursor)
        if found == -1:
            # Retry from the top: a model that reorders its segments should cost
            # precision on ordering, not recall on the claim itself.
            found = region.text.find(stripped)
        if found == -1:
            unanchored += 1
            continue
        start = region.start + found
        end = start + len(stripped)
        spans.append(
            Span(
                start=start,
                end=end,
                text=stripped,
                label=label,
                region=exempt_region if label is SpanLabel.CLAIM_EXEMPT else None,
                reason=(
                    NonExemptReason.CLASSIFIED if label is SpanLabel.CLAIM_NON_EXEMPT else None
                ),
            )
        )
        cursor = found + len(stripped)
    return spans, unanchored


def parse_segments(
    payload: str, regions: Sequence[Region], *, trace_id: str | None = None
) -> tuple[list[Span], int]:
    """Turn a tool-call payload into anchored spans.

    Args:
        payload: The tool call's ``arguments``, as JSON.
        regions: The regions rendered into the prompt, in the order they were numbered.
        trace_id: For logging.

    Returns:
        ``(spans, unanchored_count)``. A malformed payload yields no spans, which layer 3
        converts into full-region coverage gaps — the fail-closed reading.
    """
    try:
        parsed = json.loads(payload) if isinstance(payload, str) else payload
    except json.JSONDecodeError:
        log.warning("span_extractor_payload_unparseable", trace_id=trace_id)
        return [], 0

    if not isinstance(parsed, Mapping):
        log.warning("span_extractor_payload_not_an_object", trace_id=trace_id)
        return [], 0

    by_region: dict[int, list[tuple[str, SpanLabel, ExemptRegion | None]]] = {}
    for raw in parsed.get("segments") or []:
        if not isinstance(raw, Mapping):
            continue
        label = _LABEL_BY_WIRE.get(str(raw.get("label", "")))
        quote = raw.get("quote")
        index = raw.get("region")
        if label is None or not isinstance(quote, str) or not isinstance(index, int):
            continue
        exempt_region: ExemptRegion | None = None
        if label is SpanLabel.CLAIM_EXEMPT:
            # An exempt verdict naming no recognised region is not a verdict. Routed to
            # AMBIGUOUS so layer 3 pins it non-exempt, rather than being believed.
            exempt_region = _REGION_BY_WIRE.get(
                str(raw.get("exempt_region", "")), ExemptRegion.AMBIGUOUS
            )
        by_region.setdefault(index, []).append((quote, label, exempt_region))

    spans: list[Span] = []
    unanchored = 0
    for index, quotes in by_region.items():
        if not 0 <= index < len(regions):
            unanchored += len(quotes)
            continue
        region_spans, missed = _anchor_sequentially(regions[index], quotes)
        spans.extend(region_spans)
        unanchored += missed
    return spans, unanchored


class ModelSpanExtractor:
    """The classifier pass, backed by a role-bound LLM client.

    Attributes:
        client: Anything exposing ``respond(...)`` as the LLM clients do. Injected so the
            corpus harness and unit tests can drive the parsing and anchoring without a
            model.
    """

    def __init__(self, client: Any) -> None:
        """Store the client.

        Args:
            client: A client exposing an awaitable ``respond``.
        """
        self._client = client

    async def extract(self, output: str, *, user_message: str | None = None) -> SpanExtraction:
        """Partition and classify one model output.

        Args:
            output: The assistant text to classify.
            user_message: The user's turn, for judging attributed restatement.

        Returns:
            The tightened extraction. Never raises on a bad reply: a failure degrades to
            coverage gaps, which are non-exempt, which is the fail-closed direction.
        """
        regions = partition_output(output)
        # Whitespace-only regions — the blank line after a closing fence, say — cannot
        # hold a claim, and layer 3's coverage rule already labels them NOT_A_CLAIM. A
        # turn that is nothing but parse-verified code therefore costs no model call at
        # all, which matters once this runs inline on the turn path.
        classifiable = [r for r in regions if r.kind is RegionKind.CLASSIFY and r.text.strip()]
        if not classifiable:
            return apply_policy(output, regions, ())

        from personal_agent.llm_client.types import ModelRole  # noqa: PLC0415

        response = await self._client.respond(
            role=ModelRole.SPAN_EXTRACTION,
            messages=[{"role": "user", "content": build_prompt(classifiable, user_message)}],
            system_prompt=SYSTEM_PROMPT,
            tools=[span_tool()],
            tool_choice=span_tool_choice(),
            max_tokens=MAX_OUTPUT_TOKENS,
        )

        payload = ""
        for call in response.get("tool_calls") or []:
            if call.get("name") == SPAN_TOOL_NAME and call.get("arguments"):
                payload = str(call["arguments"])
                break

        spans, unanchored = parse_segments(payload, classifiable)
        if unanchored:
            log.warning("span_extractor_unanchored_quotes", count=unanchored)
        return apply_policy(output, regions, spans)


__all__ = [
    "MAX_OUTPUT_TOKENS",
    "SPAN_TOOL_NAME",
    "SYSTEM_PROMPT",
    "ModelSpanExtractor",
    "SpanExtractor",
    "build_prompt",
    "parse_segments",
    "span_tool",
    "span_tool_choice",
]
