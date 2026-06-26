"""FRE-488 — run-report aggregation + rendering tests (pure)."""

from __future__ import annotations

import json

from scripts.eval.fre435_memory_recall.attribution import Hypothesis
from scripts.eval.fre435_memory_recall.metrics import WriteOutcome
from scripts.eval.fre435_memory_recall.report import (
    CaseResult,
    RunReport,
    aggregate,
    render_json,
    render_markdown,
)


def _case(
    case_id: str,
    *,
    false_negative: bool | None,
    retrieval_miss: bool | None,
    recall: float | None,
    rr: float | None,
    ndcg: float | None,
    hypothesis: Hypothesis,
    failed: bool,
    write: WriteOutcome,
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        tags=(),
        relevant_count=1 if recall is not None else 0,
        retrieved_ids=("entity:a",),
        denied=bool(false_negative),
        recall_by_k={3: recall},
        precision_by_k={3: 0.5},
        reciprocal_rank=rr,
        ndcg_at_prod_k=ndcg,
        false_negative=false_negative,
        retrieval_miss=retrieval_miss,
        write_outcome=write,
        failed=failed,
        hypothesis=hypothesis,
    )


def _report() -> RunReport:
    cases = (
        _case(
            "hit",
            false_negative=False,
            retrieval_miss=False,
            recall=1.0,
            rr=1.0,
            ndcg=1.0,
            hypothesis=Hypothesis.PASS,
            failed=False,
            write=WriteOutcome(extraction_fired=True, entities_landed=1, entities_expected=1),
        ),
        _case(
            "denied",
            false_negative=True,
            retrieval_miss=True,
            recall=0.0,
            rr=0.0,
            ndcg=0.0,
            hypothesis=Hypothesis.H4_THRESHOLD_FN,
            failed=True,
            write=WriteOutcome(extraction_fired=True, entities_landed=1, entities_expected=1),
        ),
        _case(
            "control",
            false_negative=None,
            retrieval_miss=None,
            recall=None,
            rr=None,
            ndcg=None,
            hypothesis=Hypothesis.PASS,
            failed=False,
            write=WriteOutcome(extraction_fired=True, entities_landed=0, entities_expected=0),
        ),
    )
    return RunReport(
        run_id="seed-test",
        timestamp="2026-06-26T00:00:00+00:00",
        write_mode="replay",
        embedding_backend="zero-vector",
        prod_k=3,
        k_sweep=(1, 3),
        probe_set="seed_probe.yaml",
        cases=cases,
    )


def test_aggregate_headline_false_negative_rate() -> None:
    """Aggregate headline false negative rate."""
    agg = aggregate(_report())
    # 2 cases have a non-None false_negative (hit=False, denied=True); control excluded.
    assert agg.false_negative_rate == 0.5
    assert agg.retrieval_miss_rate == 0.5


def test_aggregate_excludes_none_recall_and_rr() -> None:
    """Aggregate excludes none recall and rr."""
    agg = aggregate(_report())
    # recall@3 over {1.0, 0.0} (control None excluded) -> 0.5
    assert agg.recall_by_k[3] == 0.5
    assert agg.mrr == 0.5  # {1.0, 0.0}


def test_aggregate_write_completeness() -> None:
    """Aggregate write completeness."""
    agg = aggregate(_report())
    assert agg.extraction_fire_rate == 1.0
    # landing: landed (1+1+0) / expected (1+1+0) = 1.0
    assert agg.landing_rate == 1.0


def test_aggregate_hypothesis_breakdown_counts_failed_only() -> None:
    """Aggregate hypothesis breakdown counts failed only."""
    agg = aggregate(_report())
    assert agg.hypothesis_counts == {"H4": 1}


def test_render_json_roundtrip() -> None:
    """Render json roundtrip."""
    payload = json.loads(render_json(_report()))
    assert payload["meta"]["embedding_backend"] == "zero-vector"
    assert payload["meta"]["write_mode"] == "replay"
    assert payload["aggregate"]["false_negative_rate"] == 0.5
    assert len(payload["cases"]) == 3


def test_render_markdown_has_headline_and_stamps() -> None:
    """Render markdown has headline and stamps."""
    md = render_markdown(_report())
    assert "false-negative rate" in md.lower()
    assert "0.50" in md  # headline value rendered
    assert "embedding_backend" in md
    assert "zero-vector" in md
    assert "H4" in md  # hypothesis breakdown present
