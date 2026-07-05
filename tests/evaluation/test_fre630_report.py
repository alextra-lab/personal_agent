"""FRE-630 — report aggregation + rendering tests (pure, no LLM)."""

from __future__ import annotations

import json

from scripts.eval.fre630_extraction_quality.gold import GoldCase, GoldEntity
from scripts.eval.fre630_extraction_quality.report import (
    CaseRun,
    RunMeta,
    RunReport,
    aggregate,
    aggregate_by_tag,
    render_json,
    render_markdown,
)
from scripts.eval.fre630_extraction_quality.scoring import score_case


def _meta(samples: int) -> RunMeta:
    return RunMeta(
        run_id="unit",
        timestamp="2026-07-03T00:00:00+00:00",
        gold_set="unit.yaml",
        extractor_model="gpt-5.4-mini",
        entity_extraction_role="gpt-5.4-mini",
        provider="openai",
        model_config_path="config/models.cloud.yaml",
        git_commit="abc1234",
        prompt_hash="deadbeef0000",
        matcher_version="1.0",
        gold_schema_version="1.0",
        samples=samples,
        fuzzy_threshold=0.86,
    )


def _case(case_id: str, tag: str) -> GoldCase:
    return GoldCase(
        case_id=case_id,
        tags=(tag,),
        source_user="u",
        source_assistant="a",
        expect_entities=(GoldEntity(name="Alpha", entity_type="Concept", knowledge_class="World"),),
        expect_relationships=(),
    )


def _perfect_result() -> dict:
    return {
        "entities": [
            {"name": "Alpha", "type": "Concept", "class": "World", "description": "One sentence."}
        ],
        "relationships": [],
        "stances": [],
        "claims": [],
    }


def _miss_result() -> dict:
    return {"entities": [], "relationships": [], "stances": [], "claims": []}


def _report() -> RunReport:
    case_a = _case("a", "physics")
    case_b = _case("b", "cooking")
    run_a = CaseRun(
        case_id="a",
        tags=("physics",),
        samples=(
            score_case(case_a, _perfect_result(), entity_type_field="v2"),
            score_case(case_a, _perfect_result(), entity_type_field="v2"),
        ),
    )
    # case b: one perfect sample, one empty-fallback sample → variance in the band
    run_b = CaseRun(
        case_id="b",
        tags=("cooking",),
        samples=(
            score_case(case_b, _perfect_result(), entity_type_field="v2"),
            score_case(case_b, _miss_result(), entity_type_field="v2"),
        ),
    )
    return RunReport(meta=_meta(samples=2), cases=(run_a, run_b))


def test_aggregate_mean_and_std() -> None:
    """Aggregate reports mean and (population) std over all sampled scores."""
    report = _report()
    agg = aggregate([s for c in report.cases for s in c.samples])
    # entity recall: a=1,1 ; b=1,0 → mean 0.75 over the 4 samples
    assert agg["entity_recall"].mean == 0.75
    assert agg["entity_recall"].std is not None and agg["entity_recall"].std > 0
    # empty_fallback_rate: one of four samples was empty → 0.25
    assert agg["empty_fallback_rate"].mean == 0.25


def test_aggregate_by_tag_partitions() -> None:
    """Per-tag aggregation buckets scores by the case's tags."""
    by_tag = aggregate_by_tag(_report())
    assert set(by_tag) == {"physics", "cooking"}
    assert by_tag["physics"]["entity_recall"].mean == 1.0
    assert by_tag["cooking"]["entity_recall"].mean == 0.5


def test_render_json_roundtrips() -> None:
    """The JSON render is valid and carries meta, aggregate, by_tag, and cases."""
    payload = json.loads(render_json(_report()))
    assert payload["meta"]["extractor_model"] == "gpt-5.4-mini"
    assert "aggregate" in payload and "by_tag" in payload
    assert len(payload["cases"]) == 2


def test_render_markdown_has_provenance_and_warning() -> None:
    """The markdown render stamps provenance and the not-statistically-powered caveat."""
    md = render_markdown(_report())
    assert "not statistically powered" in md
    assert "gpt-5.4-mini" in md
    assert "prompt_hash" in md
    assert "Per-tag" in md
