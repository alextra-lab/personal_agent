"""FRE-697 — pure unit tests for the ONNX-reranker arm (no model, no network).

Covers the pure pieces of the in-process ONNX cross-encoder benchmark:
* ``format_pair`` — the exact bge (plain) and Qwen3 seq-cls (instruct-template) pair strings;
* ``logit_to_score`` — numerically-stable sigmoid (monotone → separation-verdict-invariant);
* ``squeeze_logits`` — normalize a provider's ``[batch]`` / ``[batch,1]`` / ``[batch,2]`` output to
  one relevance logit per row, fail-loud on any other rank;
* ``OnnxCrossEncoder.score`` — dependency-injected session + tokenizer → ``sigmoid(logit)`` in input
  order; fail-loud on a row-count mismatch (no onnxruntime / no download needed);
* ``positives_negatives_for_case`` — the per-expected-entity-positive / top-non-match-negative
  extraction shared with ``_run_reranker`` (one definition for every reranker arm);
* arm-name keyspace disjointness — the ONNX dispatch branch cannot shadow an existing arm.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from scripts.eval.fre435_memory_recall.onnx_reranker import (
    DEFAULT_QWEN_INSTRUCTION,
    OnnxArm,
    OnnxCrossEncoder,
    format_pair,
    logit_to_score,
    squeeze_logits,
)


# ── format_pair ───────────────────────────────────────────────────────────────
def test_format_pair_bge_is_plain() -> None:
    """Bge is a plain (query, document) cross-encoder pair — no template."""
    a, b = format_pair("bge", "red planet?", "Mars is red.", instruction="ignored")
    assert a == "red planet?"
    assert b == "Mars is red."


def test_format_pair_qwen_seqcls_matches_model_card_template() -> None:
    """Qwen3 seq-cls uses the exact model-card system+instruct+query / document+suffix template."""
    a, b = format_pair(
        "qwen-seqcls",
        "Which planet is the Red Planet?",
        "Mars is the Red Planet.",
        instruction="INSTR",
    )
    assert a == (
        "<|im_start|>system\nJudge whether the Document meets the requirements based on the "
        'Query and the Instruct provided. Note that the answer can only be "yes" or '
        '"no".<|im_end|>\n<|im_start|>user\n'
        "<Instruct>: INSTR\n<Query>: Which planet is the Red Planet?\n"
    )
    assert b == (
        "<Document>: Mars is the Red Planet.<|im_end|>\n"
        "<|im_start|>assistant\n<think>\n\n</think>\n\n"
    )


def test_format_pair_unknown_family_raises() -> None:
    """An unknown family fails loud rather than scoring with a wrong template."""
    with pytest.raises(ValueError, match="family"):
        format_pair("mystery", "q", "d", instruction="i")


def test_default_qwen_instruction_is_model_native() -> None:
    """The default instruction is the model's trained web-search instruction (documented choice)."""
    assert DEFAULT_QWEN_INSTRUCTION == (
        "Given a web search query, retrieve relevant passages that answer the query"
    )


# ── logit_to_score ────────────────────────────────────────────────────────────
def test_logit_to_score_sigmoid_points() -> None:
    """0→0.5, monotone, saturates near 0/1."""
    assert logit_to_score(0.0) == pytest.approx(0.5)
    assert logit_to_score(2.0) > logit_to_score(1.0) > logit_to_score(0.0)
    assert logit_to_score(50.0) == pytest.approx(1.0, abs=1e-9)
    assert logit_to_score(-50.0) == pytest.approx(0.0, abs=1e-9)


def test_logit_to_score_no_overflow_on_extremes() -> None:
    """A stable sigmoid never overflows on large-magnitude logits."""
    assert 0.0 <= logit_to_score(-1000.0) < 1e-6
    assert 1.0 - logit_to_score(1000.0) < 1e-6


# ── squeeze_logits ────────────────────────────────────────────────────────────
def test_squeeze_logits_1d() -> None:
    """A [batch] output is returned as-is."""
    assert squeeze_logits(np.array([0.5, 1.2, -0.3])) == pytest.approx([0.5, 1.2, -0.3])


def test_squeeze_logits_batch_one() -> None:
    """A [batch, 1] output drops the trailing singleton."""
    assert squeeze_logits(np.array([[0.5], [1.2], [-0.3]])) == pytest.approx([0.5, 1.2, -0.3])


def test_squeeze_logits_two_class_uses_positive_log_odds() -> None:
    """A [batch, 2] output yields the positive-class log-odds (col1 - col0)."""
    assert squeeze_logits(np.array([[0.1, 0.9], [0.8, 0.2]])) == pytest.approx([0.8, -0.6])


def test_squeeze_logits_bad_rank_raises() -> None:
    """A rank-3 (or otherwise unexpected) output fails loud."""
    with pytest.raises(ValueError, match="shape|rank|logit"):
        squeeze_logits(np.zeros((2, 3, 4)))


def test_squeeze_logits_bad_last_dim_raises() -> None:
    """A [batch, 3] output (neither 1 nor 2 columns) fails loud."""
    with pytest.raises(ValueError, match="shape|column|logit"):
        squeeze_logits(np.zeros((2, 3)))


# ── OnnxCrossEncoder.score (DI: stub session + tokenizer) ─────────────────────
class _StubTokenizer:
    """Minimal tokenizer: returns fixed numpy tensors for whatever pairs it is given."""

    def __call__(self, text_a: list[str], text_b: list[str], **_: object) -> dict[str, np.ndarray]:
        n = len(text_a)
        return {
            "input_ids": np.ones((n, 4), dtype=np.int64),
            "attention_mask": np.ones((n, 4), dtype=np.int64),
        }


class _StubSession:
    """Minimal onnxruntime session stub returning fixed logits (batch, 1)."""

    def __init__(self, logits: list[float]) -> None:
        self._logits = logits

    def run(self, _outputs: object, _feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        return [np.array(self._logits, dtype=np.float32).reshape(-1, 1)]


def _di_encoder(logits: list[float]) -> OnnxCrossEncoder:
    arm = OnnxArm(
        name="stub",
        repo="stub/repo",
        revision="deadbeef",
        onnx_file="model.onnx",
        family="bge",
        quantize=False,
        precision="int8 (pre-exported)",
        instruction="",
        engine="stub",
    )
    return OnnxCrossEncoder(
        arm,
        session=_StubSession(logits),
        tokenizer=_StubTokenizer(),
        input_names=["input_ids", "attention_mask"],
        max_length=8,
    )


def test_score_maps_sigmoid_in_input_order() -> None:
    """Scores are sigmoid(logit) per document, preserving input order."""
    enc = _di_encoder([1.2, -0.3, 0.0])
    scores = enc.score("q", ["d0", "d1", "d2"])
    assert scores == pytest.approx([1 / (1 + math.exp(-1.2)), 1 / (1 + math.exp(0.3)), 0.5])


def test_score_fails_loud_on_row_count_mismatch() -> None:
    """A session returning fewer logits than documents raises (never score a partial set)."""
    enc = _di_encoder([0.9])  # 1 logit for 3 docs
    with pytest.raises(ValueError, match="count|mismatch|documents"):
        enc.score("q", ["d0", "d1", "d2"])


# ── positives_negatives_for_case (shared extraction helper) ───────────────────
def test_pos_neg_compound_positive_case() -> None:
    """Compound positive: one positive per expected entity; negative = strongest non-expected."""
    from scripts.eval.fre435_memory_recall.separation_benchmark import (
        positives_negatives_for_case,
    )

    pos, neg = positives_negatives_for_case(
        expected={"alpha", "beta"},
        cand_names=["alpha", "beta", "gamma"],
        scores=[0.8, 0.6, 0.3],
    )
    assert sorted(pos) == pytest.approx([0.6, 0.8])
    assert neg == pytest.approx(0.3)


def test_pos_neg_control_case_all_negative() -> None:
    """Control (no expected): no positives; negative = strongest of every candidate."""
    from scripts.eval.fre435_memory_recall.separation_benchmark import (
        positives_negatives_for_case,
    )

    pos, neg = positives_negatives_for_case(
        expected=set(), cand_names=["alpha", "beta"], scores=[0.2, 0.9]
    )
    assert pos == []
    assert neg == pytest.approx(0.9)


def test_pos_neg_expected_note_absent_from_shortlist() -> None:
    """An expected note the shortlist missed contributes no positive (never invents a score)."""
    from scripts.eval.fre435_memory_recall.separation_benchmark import (
        positives_negatives_for_case,
    )

    pos, neg = positives_negatives_for_case(
        expected={"alpha", "delta"}, cand_names=["alpha", "beta"], scores=[0.8, 0.6]
    )
    assert pos == pytest.approx([0.8])  # delta absent
    assert neg == pytest.approx(0.6)


# ── dispatch keyspace disjointness (routing-regression guard) ─────────────────
def test_onnx_arm_names_do_not_shadow_existing_arms() -> None:
    """ONNX arm names are disjoint from embedder + HTTP-reranker arms, so dispatch can't collide."""
    from scripts.eval.fre435_memory_recall.separation_benchmark import (
        ARMS,
        ONNX_RERANKER_ARMS,
        RERANKER_ARMS,
    )

    assert set(ONNX_RERANKER_ARMS).isdisjoint(ARMS)
    assert set(ONNX_RERANKER_ARMS).isdisjoint(RERANKER_ARMS)
    # every ONNX arm carries a matching key/name for its dict entry
    assert all(name == arm.name for name, arm in ONNX_RERANKER_ARMS.items())
