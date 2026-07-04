r"""FRE-770 — blind 3-rater classification of gold entities to the ADR-0109 V2 taxonomy.

Drives 3 model raters (gpt-5.4-mini, gpt-5.4, claude-sonnet-5) through a BLIND
classification prompt per gold entity — the entity name + its case's source
text + the ADR-0109 V2 GoLLIE type definitions, verbatim. No V1 label, no other
rater's answer, no rater identity is shown. This is a one-off research/labeling
script: it calls ``litellm.acompletion()`` DIRECTLY (not the app's cost-gated
``LiteLLMClient``) since there is no production extraction happening, just
single-turn classification — a deliberate, called-out exception documented in
docs/superpowers/plans/2026-07-04-fre-770-gold-relabel-iaa.md.

Usage::

    # dry run — stubbed raters, no real API calls, fast smoke:
    uv run python -m scripts.eval.fre630_extraction_quality.relabel_v2_types \
        --run-id smoke --dry-run

    # real run — all entities, 3 real model raters:
    uv run python -m scripts.eval.fre630_extraction_quality.relabel_v2_types \
        --run-id fre770-2026-07-04

Raw per-entity/per-rater records land in the gitignored
``telemetry/evaluation/fre630-extraction-quality/v2-relabel-<run-id>.json``.
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
    ALLOWED_ENTITY_TYPES_V2,
    GoldCase,
    load_gold_set,
)
from scripts.eval.fre630_extraction_quality.iaa import IAAReport, build_iaa_report

from personal_agent.config import settings

log = structlog.get_logger(__name__)

DEFAULT_GOLD_SET = "scripts/eval/fre630_extraction_quality/gold_extraction.yaml"
DEFAULT_OUT_DIR = Path("telemetry/evaluation/fre630-extraction-quality")

#: ADR-0109 V2 GoLLIE-style definitions (inclusion + exclusion + example),
#: copied VERBATIM from docs/architecture_decisions/ADR-0109-entity-taxonomy-redesign.md.
#: Keep in sync by hand if the ADR's table changes — `prompt_hash()` pins
#: whichever version a given relabel run actually used.
V2_TYPE_DEFINITIONS: dict[str, str] = {
    "Person": (
        'a real, named individual human. Not "User"/"Assistant", generic roles, teams, orgs.'
    ),
    "Organization": (
        "a named company, institution, agency, department, team, or standards body. "
        "Not software products or locations."
    ),
    "Location": (
        "a named geographic or physical place. Not organizations named after places, "
        "namespaces, repos."
    ),
    "TechnicalArtifact": (
        "a concrete, named engineered/built thing you can install, run, call, configure, "
        "or physically use — software or hardware. Not an abstract method/idea "
        "(-> MethodOrConcept). e.g. Python, Neo4j, FastAPI, a GPU, an oscilloscope."
    ),
    "MethodOrConcept": (
        "a specific human-invented abstract idea, method, technique, algorithm, data "
        "structure, pattern, or principle. Not a built artifact; not a broad field; not a "
        "natural phenomenon. e.g. GraphRAG, trie, Nash equilibrium, retrieval-augmented "
        "generation."
    ),
    "DomainOrTopic": (
        "a broad field, domain, discipline, or subject area as a whole. Not a specific "
        "technique within it (-> MethodOrConcept). e.g. behavioral economics, cosmology, "
        "cybersecurity, game theory."
    ),
    "Phenomenon": (
        "a naturally-occurring physical/natural phenomenon, process, effect, force, or "
        "observable that exists independently of human design. Not a human-invented method "
        "(-> MethodOrConcept). e.g. cosmic microwave background, gravity, photosynthesis, "
        "the greenhouse effect, the Maillard reaction."
    ),
    "Event": (
        "a specific named occurrence, milestone, incident, release, or time-bound activity. "
        "e.g. the Big Bang, ICLR 2024, a production outage."
    ),
}


@dataclass(frozen=True)
class Rater:
    """One blind-classification model rater.

    Attributes:
        name: Short id (report column / rater-pair label).
        provider: ``"openai"`` | ``"anthropic"`` (selects the litellm prefix + api key).
        model_id: The bare model id (unprefixed).
        temperature: Sampling temperature, or ``None`` for the provider default.
    """

    name: str
    provider: str
    model_id: str
    temperature: float | None


#: The 3 model raters across two provider families — same ids/pricing as
#: cells.py's FRE-766 matrix, so they are already known-good in this codebase.
RATERS: tuple[Rater, ...] = (
    Rater(name="mini", provider="openai", model_id="gpt-5.4-mini", temperature=0.0),
    Rater(name="full", provider="openai", model_id="gpt-5.4", temperature=0.0),
    Rater(name="sonnet", provider="anthropic", model_id="claude-sonnet-5", temperature=None),
)


@dataclass(frozen=True)
class EntityItem:
    """One (case, entity) pair to classify.

    Attributes:
        item_id: ``"<case_id>::<entity_name>"`` — the report/adjudication key.
        case_id: Owning gold case id.
        entity_name: The entity's canonical name.
        context: The case's combined source_user + source_assistant text.
    """

    item_id: str
    case_id: str
    entity_name: str
    context: str


def collect_entity_items(cases: Sequence[GoldCase]) -> list[EntityItem]:
    """Flatten every gold case's entities into blind-classification items.

    Args:
        cases: The loaded gold cases.

    Returns:
        One :class:`EntityItem` per (case, entity) pair, in file order.
    """
    items: list[EntityItem] = []
    for case in cases:
        context = f"{case.source_user}\n{case.source_assistant}".strip()
        for entity in case.expect_entities:
            items.append(
                EntityItem(
                    item_id=f"{case.case_id}::{entity.name}",
                    case_id=case.case_id,
                    entity_name=entity.name,
                    context=context,
                )
            )
    return items


def _classification_prompt(entity_name: str, context: str) -> str:
    """Build the blind classification prompt for one entity.

    Args:
        entity_name: The entity's canonical name (no V1 type shown — blind).
        context: The owning case's source text.

    Returns:
        The full prompt text.
    """
    definitions = "\n".join(f"- {key}: {desc}" for key, desc in V2_TYPE_DEFINITIONS.items())
    return (
        "Classify the ENTITY below into exactly one of these 8 types. Each definition "
        "states what to INCLUDE and EXCLUDE, with an example.\n\n"
        f"{definitions}\n\n"
        f"CONTEXT (the conversation the entity was mentioned in):\n{context}\n\n"
        f'ENTITY: "{entity_name}"\n\n'
        "Reply with ONLY a JSON object: "
        '{"type": "<one of the 8 keys above>", "rationale": "<one short sentence>"}'
    )


def prompt_hash() -> str:
    """Stable hash of the classification prompt template + type definitions.

    Returns:
        A short hex digest so a relabel run is never misread against a
        different prompt/definition revision later.
    """
    template = _classification_prompt("<ENTITY>", "<CONTEXT>")
    return hashlib.sha256(template.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class RaterResponse:
    """One rater's parsed answer for one entity.

    Attributes:
        type_label: The rater's chosen V2 type (empty string on parse failure).
        rationale: The rater's one-line rationale (empty on parse failure).
        raw_text: The raw response text (kept for the telemetry record).
        error: Provider/parse error class, if any.
    """

    type_label: str
    rationale: str
    raw_text: str
    error: str | None = None


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_rater_response(raw_text: str) -> RaterResponse:
    """Tolerantly parse a rater's JSON response.

    Args:
        raw_text: The raw model response text.

    Returns:
        The parsed :class:`RaterResponse`; ``type_label=""`` and ``error`` set
        on any parse failure or off-vocabulary type (never raises — a
        malformed rater response is a data point, not a crash).
    """
    match = _JSON_OBJECT_RE.search(raw_text)
    if not match:
        return RaterResponse(type_label="", rationale="", raw_text=raw_text, error="no_json_found")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return RaterResponse(
            type_label="", rationale="", raw_text=raw_text, error="json_decode_error"
        )
    type_label = str(parsed.get("type", "")).strip()
    rationale = str(parsed.get("rationale", "")).strip()
    if type_label not in ALLOWED_ENTITY_TYPES_V2:
        return RaterResponse(
            type_label="", rationale=rationale, raw_text=raw_text, error="off_vocab_type"
        )
    return RaterResponse(type_label=type_label, rationale=rationale, raw_text=raw_text)


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
        return RaterResponse(type_label="", rationale="", raw_text="", error=type(exc).__name__)
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
        type_label="MethodOrConcept", rationale=f"dry-run stub for {rater.name}", raw_text="{}"
    )


async def classify_all(
    items: Sequence[EntityItem], *, dry_run: bool
) -> dict[str, dict[str, RaterResponse]]:
    """Classify every entity item with every rater, concurrently.

    Args:
        items: The entities to classify.
        dry_run: If True, use stubbed responses instead of real API calls.

    Returns:
        ``{item_id: {rater_name: RaterResponse}}``.
    """

    async def _one(item: EntityItem, rater: Rater) -> tuple[str, str, RaterResponse]:
        if dry_run:
            return item.item_id, rater.name, _dry_run_response(rater)
        prompt = _classification_prompt(item.entity_name, item.context)
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
        "type": response.type_label,
        "rationale": response.rationale,
        "raw_text": response.raw_text,
        "error": response.error,
    }


def write_raw_telemetry(
    run_id: str,
    items: Sequence[EntityItem],
    by_item: dict[str, dict[str, RaterResponse]],
    out_dir: Path = DEFAULT_OUT_DIR,
) -> Path:
    """Write the raw per-entity/per-rater records (gitignored telemetry).

    Args:
        run_id: This run's id (names the output file).
        items: The classified entity items.
        by_item: The rater responses, keyed by item id then rater name.
        out_dir: Output directory (created if missing).

    Returns:
        The path written.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"v2-relabel-{run_id}.json"
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
                "entity_name": item.entity_name,
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
    items: Sequence[EntityItem], by_item: dict[str, dict[str, RaterResponse]]
) -> IAAReport:
    """Build the :class:`IAAReport` from classified items.

    Args:
        items: The classified entity items, in a fixed order.
        by_item: The rater responses, keyed by item id then rater name.

    Returns:
        The assembled :class:`IAAReport`, computed only over items where every
        rater returned a valid (non-error) label.
    """
    rater_names = [r.name for r in RATERS]
    empty = RaterResponse(type_label="", rationale="", raw_text="")
    complete_items = [
        item
        for item in items
        if all(by_item.get(item.item_id, {}).get(name, empty).type_label for name in rater_names)
    ]
    rater_labels = [
        [by_item[item.item_id][name].type_label for name in rater_names] for item in complete_items
    ]
    item_ids = [item.item_id for item in complete_items]
    return build_iaa_report(
        rater_labels=rater_labels,
        item_ids=item_ids,
        rater_names=rater_names,
        categories=sorted(ALLOWED_ENTITY_TYPES_V2),
    )


def render_report_table(report: IAAReport) -> str:
    """Render a curated, pasteable per-type IAA table + rater-pair table.

    Args:
        report: The assembled report.

    Returns:
        A markdown-table string for the research doc.
    """
    lines = ["| type | kappa | status | n_positive | raw_agreement |", "|---|---|---|---|---|"]
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
    items = collect_entity_items(cases)
    if args.limit:
        items = items[: args.limit]

    log.info("relabel_run_start", run_id=args.run_id, dry_run=args.dry_run, n_items=len(items))
    by_item = asyncio.run(classify_all(items, dry_run=args.dry_run))
    out_path = write_raw_telemetry(args.run_id, items, by_item)
    report = build_report(items, by_item)
    print(render_report_table(report))
    log.info("relabel_run_complete", run_id=args.run_id, out_path=str(out_path))


if __name__ == "__main__":
    main()
