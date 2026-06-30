"""FRE-696 — production reranker config: switch to the reliable MLX 4B mxfp8 + bound input cap.

FRE-695/696 found the local llama.cpp Qwen3-Reranker stalls under sustained candidate load
(and times out at the 50-candidate cap), while the MLX runtime is reliable. The MLX 4B mxfp8
also out-separates the llama.cpp 4B (Youden's J 0.747 vs 0.71) at the same latency class, and
the FRE-696 latency curve (~0.11s/candidate) motivated lowering the rerank input cap 50 -> 25.

These assert the config *outcome* (the prod pointer and the cap), so an accidental revert to the
stalling llama.cpp reranker — or to the old 50 cap — fails loudly.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from personal_agent.config.settings import AppConfig


def test_cloud_reranker_is_mlx_4b_mxfp8() -> None:
    """The production (cloud) reranker points at the reliable MLX 4B mxfp8, not llama.cpp."""
    cfg = yaml.safe_load(Path("config/models.cloud.yaml").read_text())
    reranker = cfg["models"]["reranker"]
    assert reranker["id"] == "Qwen/Qwen3-Reranker-4B-mxfp8"
    assert reranker["endpoint"] == "https://slm.frenchforet.com/v1"


def test_reranker_input_cap_default_is_25() -> None:
    """FRE-696 lowered the rerank input cap 50 -> 25 to bound per-recall latency (~2.8s).

    Asserted on the field default (not an instantiated Settings) so a live .env does not
    perturb the check.
    """
    assert AppConfig.model_fields["reranker_input_cap"].default == 25
