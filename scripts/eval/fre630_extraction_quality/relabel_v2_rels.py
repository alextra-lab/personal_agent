r"""FRE-773 — blind 3-rater classification of gold relationships to the ADR-0109 V2 vocab.

The relationship-axis mirror of ``relabel_v2_types.py``. Drives 3 model raters
(gpt-5.4-mini, gpt-5.4, claude-sonnet-5 — the same panel) through a BLIND
classification prompt per gold relationship: the ordered ``source -> target``
pair + its case's source text + the ADR-0109 V2 relationship definitions
(directional, GoLLIE inclusion/exclusion, RELATED_TO gated as a last-resort
None-of-the-Above). No V1 label, no other rater's answer, no rater identity is
shown. The rater may also answer ``NONE`` (the emit-nothing-if-none-fits rule) —
captured as a distinct outcome, never coerced into a real type.

Like ``relabel_v2_types.py`` this is a one-off research/labeling script: it calls
``litellm.acompletion()`` DIRECTLY (not the app's cost-gated ``LiteLLMClient``)
since there is no production extraction happening, just single-turn
classification — the same deliberate, called-out exception documented in
docs/superpowers/plans/2026-07-04-fre-773-relationship-v2-validation.md.

Usage::

    # dry run — stubbed raters, no real API calls, fast smoke:
    uv run python -m scripts.eval.fre630_extraction_quality.relabel_v2_rels \
        --run-id smoke --dry-run

    # real run — all relationships, 3 real model raters:
    uv run python -m scripts.eval.fre630_extraction_quality.relabel_v2_rels \
        --run-id fre773-2026-07-04

Raw per-relationship/per-rater records land in the gitignored
``telemetry/evaluation/fre630-extraction-quality/v2-rel-relabel-<run-id>.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from scripts.eval.fre630_extraction_quality.gold import (
    ALLOWED_REL_TYPES_V2,
    REL_V2_NO_EDGE,
    GoldCase,
    load_gold_set,
)
from scripts.eval.fre630_extraction_quality.iaa import IAAReport, build_iaa_report
from scripts.eval.fre630_extraction_quality.relabel_v2_types import RATERS, Rater

from personal_agent.config import settings

log = structlog.get_logger(__name__)

DEFAULT_GOLD_SET = "scripts/eval/fre630_extraction_quality/gold_extraction.yaml"
DEFAULT_OUT_DIR = Path("telemetry/evaluation/fre630-extraction-quality")

#: ADR-0109 V2 relationship definitions (FRE-773): directional, GoLLIE-style
#: (inclusion + exclusion + example), with RELATED_TO recast as a gated
#: last-resort None-of-the-Above. Authored for this ticket from the ADR's stated
#: approach (§ V2 relationship types (a)/(b)/(c)) — the ADR gives the approach,
#: not finished per-relation text, unlike the entity table. `prompt_hash()` pins
#: whichever revision a given relabel run actually used.
V2_REL_DEFINITIONS: dict[str, str] = {
    "PART_OF": (
        "SOURCE is a structural component, member, stage, or constituent OF THE WHOLE "
        "TARGET — source is literally a piece of target. Not a functional dependency "
        "(-> USES); not 'source is a concept or method merely studied within the field "
        "target' (topical containment of an idea inside a subject area is not structural "
        "membership -> RELATED_TO or NONE). e.g. Containment PART_OF Incident Response; "
        "Interval Recognition PART_OF Ear Training."
    ),
    "USES": (
        "SOURCE functionally depends on, invokes, consumes, or is built on TARGET to "
        "operate — source requires target to work. Directional: if instead target is "
        "merely 'used for' source, or the dependency runs the other way, or it is only a "
        "loose association, do not use USES (-> RELATED_TO). Not similarity "
        "(-> SIMILAR_TO); not part/whole (-> PART_OF). e.g. FastAPI USES PostgreSQL."
    ),
    "CREATED_BY": (
        "SOURCE (an artifact, work, or product) was authored, invented, produced, or "
        "originated BY TARGET (a person or organization). Not use or membership. "
        "e.g. Linux CREATED_BY Linus Torvalds."
    ),
    "LOCATED_IN": (
        "SOURCE is geographically or physically situated within the place TARGET. Not "
        "organizational membership (-> PART_OF); not topical containment. e.g. Alhambra "
        "LOCATED_IN Granada."
    ),
    "SIMILAR_TO": (
        "SOURCE and TARGET are comparable, analogous, or near-equivalent alternatives at "
        "the same level of abstraction (symmetric — direction does not matter). Not one "
        "depending on the other (-> USES); not part/whole (-> PART_OF). e.g. PostgreSQL "
        "SIMILAR_TO MySQL."
    ),
    "RELATED_TO": (
        "GATED LAST RESORT (None-of-the-Above). Use ONLY when SOURCE and TARGET are "
        "clearly associated but NO specific type above applies — never when a specific "
        "type fits. If the association is weak or topical and no directional type holds, "
        "use RELATED_TO; if nothing meaningful connects them, answer NONE instead. "
        "e.g. Cosmic Microwave Background RELATED_TO Big Bang (an evidence-of "
        "association, not part/use/creation)."
    ),
}

#: The IAA category set: the 6 V2 relationship keys plus the explicit NONE outcome
#: (the emit-nothing-if-none-fits rule). NONE participates as its own category so a
#: rater splitting between a type and "no edge" registers as a genuine disagreement,
#: never a silent coercion.
REL_CLASSIFY_CATEGORIES: tuple[str, ...] = (*sorted(ALLOWED_REL_TYPES_V2), REL_V2_NO_EDGE)


@dataclass(frozen=True)
class RelItem:
    """One (case, source->target) relationship pair to classify.

    Attributes:
        item_id: ``"<case_id>::<source>-><target>"`` — the report/adjudication key.
        case_id: Owning gold case id.
        source: The relationship's source gold entity canonical name.
        target: The relationship's target gold entity canonical name.
        context: The case's combined source_user + source_assistant text.
    """

    item_id: str
    case_id: str
    source: str
    target: str
    context: str


def collect_rel_items(cases: Sequence[GoldCase]) -> list[RelItem]:
    """Flatten every gold case's relationships into blind-classification items.

    Args:
        cases: The loaded gold cases.

    Returns:
        One :class:`RelItem` per (case, relationship) pair, in file order.
    """
    items: list[RelItem] = []
    for case in cases:
        context = f"{case.source_user}\n{case.source_assistant}".strip()
        for rel in case.expect_relationships:
            items.append(
                RelItem(
                    item_id=f"{case.case_id}::{rel.source}->{rel.target}",
                    case_id=case.case_id,
                    source=rel.source,
                    target=rel.target,
                    context=context,
                )
            )
    return items


def _classification_prompt(source: str, target: str, context: str) -> str:
    """Build the blind relationship-classification prompt for one ordered pair.

    Args:
        source: The relationship's source entity name (no V1 label shown — blind).
        target: The relationship's target entity name.
        context: The owning case's source text.

    Returns:
        The full prompt text.
    """
    definitions = "\n".join(f"- {key}: {desc}" for key, desc in V2_REL_DEFINITIONS.items())
    return (
        "Classify the RELATIONSHIP between the ordered pair of entities below into exactly "
        "one type. The direction is FIXED: judge whether 'SOURCE <TYPE> TARGET' holds as "
        "written. Each definition states what to INCLUDE and EXCLUDE, with an example.\n\n"
        f"{definitions}\n\n"
        "RELATED_TO is a GATED LAST RESORT: use it ONLY when the pair is clearly associated "
        "but no specific type above applies — never when a specific type fits.\n"
        "If NO relationship (not even a weak association) genuinely holds in the "
        "SOURCE->TARGET direction, answer NONE.\n\n"
        f"CONTEXT (the conversation the pair was mentioned in):\n{context}\n\n"
        f'SOURCE: "{source}"\nTARGET: "{target}"\n\n'
        "Reply with ONLY a JSON object: "
        '{"type": "<one of the type keys above, or NONE>", "rationale": "<one short sentence>"}'
    )


def prompt_hash() -> str:
    """Stable hash of the classification prompt template + relationship definitions.

    Returns:
        A short hex digest so a relabel run is never misread against a different
        prompt/definition revision later.
    """
    template = _classification_prompt("<SOURCE>", "<TARGET>", "<CONTEXT>")
    return hashlib.sha256(template.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class RaterResponse:
    """One rater's parsed answer for one relationship.

    Attributes:
        rel_label: The rater's chosen V2 rel type or ``NONE`` (empty string on
            parse failure).
        rationale: The rater's one-line rationale (empty on parse failure).
        raw_text: The raw response text (kept for the telemetry record).
        error: Provider/parse error class, if any.
    """

    rel_label: str
    rationale: str
    raw_text: str
    error: str | None = None


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_rater_response(raw_text: str) -> RaterResponse:
    """Tolerantly parse a rater's JSON response.

    Args:
        raw_text: The raw model response text.

    Returns:
        The parsed :class:`RaterResponse`; ``rel_label=""`` and ``error`` set on
        any parse failure or off-vocabulary label (never raises — a malformed
        rater response is a data point, not a crash). A valid label is one of the
        6 V2 keys or the ``REL_V2_NO_EDGE`` marker.
    """
    match = _JSON_OBJECT_RE.search(raw_text)
    if not match:
        return RaterResponse(rel_label="", rationale="", raw_text=raw_text, error="no_json_found")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return RaterResponse(
            rel_label="", rationale="", raw_text=raw_text, error="json_decode_error"
        )
    rel_label = str(parsed.get("type", "")).strip()
    rationale = str(parsed.get("rationale", "")).strip()
    if rel_label not in ALLOWED_REL_TYPES_V2 and rel_label != REL_V2_NO_EDGE:
        return RaterResponse(
            rel_label="", rationale=rationale, raw_text=raw_text, error="off_vocab_type"
        )
    return RaterResponse(rel_label=rel_label, rationale=rationale, raw_text=raw_text)


async def _call_rater(rater: Rater, prompt: str) -> RaterResponse:
    """Call one rater directly via litellm (bypassing the app's cost gate).

    Args:
        rater: The rater to call.
        prompt: The classification prompt.

    Returns:
        The parsed :class:`RaterResponse` (never raises — a provider error is
        recorded as an ``error`` field, not propagated, so one rater's outage
        doesn't crash the whole run).
    """
    import litellm

    api_key = (
        settings.anthropic_api_key if rater.provider == "anthropic" else settings.openai_api_key
    )
    kwargs: dict[str, Any] = {
        "model": f"{rater.provider}/{rater.model_id}",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200,
    }
    if rater.temperature is not None:
        kwargs["temperature"] = rater.temperature
    if api_key:
        kwargs["api_key"] = api_key
    try:
        response = await litellm.acompletion(**kwargs)
    except Exception as exc:  # noqa: BLE001 — a rater outage is a data point, not a crash
        return RaterResponse(rel_label="", rationale="", raw_text="", error=type(exc).__name__)
    text = response.choices[0].message.content or ""
    return _parse_rater_response(text)


def _dry_run_response(rater: Rater) -> RaterResponse:
    """Deterministic stub response for ``--dry-run`` (no real API calls).

    Args:
        rater: The rater being stubbed.

    Returns:
        A fixed, schema-valid :class:`RaterResponse` for fast, cost-free smoke
        testing of the plumbing.
    """
    return RaterResponse(
        rel_label="RELATED_TO", rationale=f"dry-run stub for {rater.name}", raw_text="{}"
    )


async def classify_all(
    items: Sequence[RelItem], *, dry_run: bool
) -> dict[str, dict[str, RaterResponse]]:
    """Classify every relationship item with every rater, concurrently.

    Args:
        items: The relationships to classify.
        dry_run: If True, use stubbed responses instead of real API calls.

    Returns:
        ``{item_id: {rater_name: RaterResponse}}``.
    """

    async def _one(item: RelItem, rater: Rater) -> tuple[str, str, RaterResponse]:
        if dry_run:
            return item.item_id, rater.name, _dry_run_response(rater)
        prompt = _classification_prompt(item.source, item.target, item.context)
        return item.item_id, rater.name, await _call_rater(rater, prompt)

    tasks = [_one(item, rater) for item in items for rater in RATERS]
    results = await asyncio.gather(*tasks)

    by_item: dict[str, dict[str, RaterResponse]] = {}
    for item_id, rater_name, response in results:
        by_item.setdefault(item_id, {})[rater_name] = response
    return by_item


def _serialize(response: RaterResponse) -> dict[str, Any]:
    """Serialize one rater response for the raw telemetry record.

    Args:
        response: The rater response.

    Returns:
        A JSON-serializable dict.
    """
    return {
        "type": response.rel_label,
        "rationale": response.rationale,
        "raw_text": response.raw_text,
        "error": response.error,
    }


def write_raw_telemetry(
    run_id: str,
    items: Sequence[RelItem],
    by_item: dict[str, dict[str, RaterResponse]],
    out_dir: Path = DEFAULT_OUT_DIR,
) -> Path:
    """Write the raw per-relationship/per-rater records (gitignored telemetry).

    Args:
        run_id: This run's id (names the output file).
        items: The classified relationship items.
        by_item: The rater responses, keyed by item id then rater name.
        out_dir: Output directory (created if missing).

    Returns:
        The path written.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"v2-rel-relabel-{run_id}.json"
    record = {
        "run_id": run_id,
        "prompt_hash": prompt_hash(),
        "raters": [
            {"name": r.name, "provider": r.provider, "model_id": r.model_id} for r in RATERS
        ],
        "items": [
            {
                "item_id": item.item_id,
                "case_id": item.case_id,
                "source": item.source,
                "target": item.target,
                "responses": {
                    rater_name: _serialize(resp)
                    for rater_name, resp in by_item.get(item.item_id, {}).items()
                },
            }
            for item in items
        ],
    }
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def build_report(
    items: Sequence[RelItem], by_item: dict[str, dict[str, RaterResponse]]
) -> IAAReport:
    """Build the :class:`IAAReport` from classified relationships.

    Args:
        items: The classified relationship items, in a fixed order.
        by_item: The rater responses, keyed by item id then rater name.

    Returns:
        The assembled :class:`IAAReport`, computed only over items where every
        rater returned a valid (non-error) label.
    """
    rater_names = [r.name for r in RATERS]
    empty = RaterResponse(rel_label="", rationale="", raw_text="")
    complete_items = [
        item
        for item in items
        if all(by_item.get(item.item_id, {}).get(name, empty).rel_label for name in rater_names)
    ]
    rater_labels = [
        [by_item[item.item_id][name].rel_label for name in rater_names] for item in complete_items
    ]
    item_ids = [item.item_id for item in complete_items]
    return build_iaa_report(
        rater_labels=rater_labels,
        item_ids=item_ids,
        rater_names=rater_names,
        categories=REL_CLASSIFY_CATEGORIES,
    )


def render_report_table(report: IAAReport) -> str:
    """Render a curated, pasteable per-type IAA table + rater-pair table.

    Args:
        report: The assembled report.

    Returns:
        A markdown-table string for the research doc.
    """
    lines = ["| rel type | kappa | status | n_positive | raw_agreement |", "|---|---|---|---|---|"]
    for type_name, result in sorted(report.per_type.items()):
        kappa_str = f"{result.kappa:.3f}" if result.kappa is not None else "—"
        lines.append(
            f"| {type_name} | {kappa_str} | {result.status} | {result.n_positive} | "
            f"{result.raw_agreement:.3f} |"
        )
    overall_kappa = f"{report.overall.kappa:.3f}" if report.overall.kappa is not None else "—"
    lines.append("")
    lines.append(
        f"Overall kappa: {overall_kappa} ({report.overall.status}, n={report.overall.n_items})"
    )
    lines.append("")
    lines.append("| rater pair | agreement |")
    lines.append("|---|---|")
    for (a, b), agreement in sorted(report.by_rater_pair.items()):
        lines.append(f"| {a}↔{b} | {agreement:.3f} |")
    lines.append("")
    lines.append(
        f"Disagreements ({len(report.disagreements)}): {', '.join(report.disagreements) or 'none'}"
    )
    return "\n".join(lines)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--dry-run", action="store_true", help="Stub raters, no real API calls")
    parser.add_argument("--limit", type=int, default=None, help="Classify only the first N items")
    args = parser.parse_args()

    cases = load_gold_set(DEFAULT_GOLD_SET)
    items = collect_rel_items(cases)
    if args.limit:
        items = items[: args.limit]

    log.info("rel_relabel_run_start", run_id=args.run_id, dry_run=args.dry_run, n_items=len(items))
    by_item = asyncio.run(classify_all(items, dry_run=args.dry_run))
    out_path = write_raw_telemetry(args.run_id, items, by_item)
    print(render_report_table(report := build_report(items, by_item)))
    log.info(
        "rel_relabel_run_complete",
        run_id=args.run_id,
        out_path=str(out_path),
        overall_kappa=report.overall.kappa,
        n_items=report.overall.n_items,
    )


if __name__ == "__main__":
    main()
