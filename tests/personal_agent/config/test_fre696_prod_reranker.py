"""FRE-696/FRE-851 — production reranker config lineage.

FRE-695/696 found the local llama.cpp Qwen3-Reranker stalls under sustained candidate load
(and times out at the 50-candidate cap), while the MLX runtime is reliable. The MLX 4B mxfp8
also out-separates the llama.cpp 4B (Youden's J 0.747 vs 0.71) at the same latency class, and
the FRE-696 latency curve (~0.11s/candidate) motivated lowering the rerank input cap 50 -> 25.

FRE-851 then moved the "reranker" role's PRIMARY target to Voyage rerank-2.5 (managed, ~250ms,
quality-equivalent per FRE-695's stored matrix) and retired the MLX 4B mxfp8 to a programmatic
fallback target under the "reranker_fallback" role (memory/reranker.py falls back to it on a
Voyage error/timeout).

These assert the config *outcome* (the prod pointers and the cap), so an accidental revert to the
stalling llama.cpp reranker, a dropped fallback target, or the old 50 cap all fail loudly.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from personal_agent.config.settings import AppConfig


def test_reranker_is_voyage_rerank_2_5() -> None:
    """The "reranker" role's PRIMARY target is Voyage rerank-2.5 (FRE-851)."""
    cfg = yaml.safe_load(Path("config/models.yaml").read_text())
    reranker = cfg["models"]["reranker"]
    assert reranker["id"] == "rerank-2.5"
    assert reranker["endpoint"] == "https://api.voyageai.com/v1"


def test_reranker_fallback_is_mlx_4b_mxfp8() -> None:
    """The "reranker_fallback" role — the programmatic fallback target — is the reliable MLX 4B mxfp8."""
    cfg = yaml.safe_load(Path("config/models.yaml").read_text())
    fallback = cfg["models"]["reranker_fallback"]
    assert fallback["id"] == "Qwen/Qwen3-Reranker-4B-mxfp8"
    assert fallback["endpoint"] == "https://slm.example.com/v1"


def test_reranker_input_cap_default_is_25() -> None:
    """FRE-696 lowered the rerank input cap 50 -> 25 to bound per-recall latency (~2.8s).

    Asserted on the field default (not an instantiated Settings) so a live .env does not
    perturb the check.
    """
    assert AppConfig.model_fields["reranker_input_cap"].default == 25
