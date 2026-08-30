"""Confusion matrix build/render + the AC-1/AC-5 loud-failure gates, as pure logic."""

from __future__ import annotations

from scripts.eval.fre1337_intent_probe.harness import (
    ProbeRow,
    build_confusion_matrix,
    has_seeded_agreement,
    render_confusion_markdown,
)


def _row(model_key: str, det: str, model_answer: str, fixture: str = "f") -> ProbeRow:
    return ProbeRow(
        fixture_label=fixture,
        model_key=model_key,
        deterministic_task_type=det,
        deterministic_confidence=0.7,
        deterministic_signals=["no_special_patterns"],
        model_task_type=model_answer,
        model_reason="because",
        prompt="p",
        resolved_model_id="m-id",
        requested_model_id="m-id",
    )


def test_matrix_counts_agreement_and_disagreement_cells() -> None:
    rows = [
        _row("sonnet", "conversational", "conversational"),  # agree
        _row("sonnet", "conversational", "analysis"),  # disagree
        _row("sonnet", "conversational", "analysis"),  # disagree, same cell
    ]
    matrix = build_confusion_matrix(rows)
    assert matrix["sonnet"][("conversational", "conversational")] == 1
    assert matrix["sonnet"][("conversational", "analysis")] == 2


def test_matrix_is_per_model() -> None:
    rows = [
        _row("sonnet", "conversational", "conversational"),
        _row("qwen", "conversational", "analysis"),
    ]
    matrix = build_confusion_matrix(rows)
    assert set(matrix) == {"sonnet", "qwen"}
    assert ("conversational", "analysis") not in matrix["sonnet"]


def test_has_seeded_agreement_true_when_a_diagonal_cell_exists() -> None:
    matrix = build_confusion_matrix([_row("sonnet", "conversational", "conversational")])
    assert has_seeded_agreement(matrix)


def test_has_seeded_agreement_false_when_every_cell_disagrees() -> None:
    matrix = build_confusion_matrix(
        [
            _row("sonnet", "conversational", "analysis"),
            _row("qwen", "conversational", "tool_use"),
        ]
    )
    assert not has_seeded_agreement(matrix)


def test_render_markdown_includes_every_model_and_marks_agreement() -> None:
    matrix = build_confusion_matrix(
        [
            _row("sonnet", "conversational", "conversational"),
            _row("sonnet", "conversational", "analysis"),
        ]
    )
    rendered = render_confusion_markdown(matrix)
    assert "## sonnet" in rendered
    assert "(agree)" in rendered
    assert "conversational | analysis |" in rendered
