"""FRE-694 — offline embedder-separation benchmark over the FRE-670 probe.

The FRE-694 question: does a higher-quality embedder open a *clean floor* — a cosine
cutoff that separates true matches (positives) from no-record (negatives) — on the
FRE-670 vocabulary-divergent probe? Recall@5 saturates and hides this; separation is
the metric. This runs the ceiling sweep at f16 across the board so size is the only
variable (the prior 4B arm ran at Q4, which perturbs the fine cosine geometry).

OFFLINE BY DESIGN (FRE-694, owner-approved): cosines are computed directly
(embed -> truncate -> renormalize -> dot), touching NO substrate — the only way to
sweep dimensions (the Neo4j vector index is single-dimension) and to run a cloud arm.
It answers "does the exact embedding geometry contain a clean floor?", NOT "will the
production Neo4j HNSW path retrieve at that floor?" — a 0.6B@1024 parity check against
the FRE-670 calibrate medians validates the harness before any cross-arm number is
trusted (run with --parity-only).

Entity / query text mirrors production exactly (parity): an entity embeds as
``"{name}: {description}"`` in document mode; a query embeds in query mode (the Qwen
instruction prefix for the local arms, Voyage ``input_type`` for the cloud arm).

Arms:
  * ``0.6b`` / ``8b`` — local Qwen3-Embedding via ``generate_embedding`` (config +
    AGENT_EMBEDDING_DIMENSIONS pinned by run_embedder_benchmark.sh). f16.
  * ``voyage`` — voyage-4-large via the Voyage API; key read from ``pass show
    VOYAGEAI_API_KEY`` at run time (never written to disk, never logged).

Matryoshka reduction is client-side first-N + L2 renormalize for every arm
(validated: Voyage client-trunc vs server output_dimension cosine ~0.999); each is the
provider's intended MRL reduction.

Run::

    # local arms via the wrapper (pins config + dims + CF token + preflight)
    run_embedder_benchmark.sh 0.6b separation --probe scripts/eval/.../semantic_probe.yaml
    run_embedder_benchmark.sh 8b   separation --probe ...
    # cloud arm directly (no substrate, no wrapper)
    uv run python scripts/eval/fre435_memory_recall/separation_benchmark.py --arm voyage
"""

from __future__ import annotations

import os

# Pin the TEST substrate before any personal_agent import (settings is an import-time
# singleton). The harness never connects to a substrate, but this keeps a direct
# invocation off prod config and past the ADR-0099 validator. ``setdefault`` lets the
# wrapper's force-exports win. (Mirrors harness.py.)
for _key, _value in {
    "APP_ENV": "test",
    "AGENT_MODEL_CONFIG_PATH": "config/models.yaml",
    "AGENT_NEO4J_URI": "bolt://localhost:7688",
    "AGENT_ELASTICSEARCH_URL": "http://localhost:9201",
    "AGENT_DATABASE_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
}.items():
    os.environ.setdefault(_key, _value)

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
from collections.abc import Sequence  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
from scripts.eval.fre435_memory_recall.calibration import propose_floor, sweep_floor  # noqa: E402
from scripts.eval.fre435_memory_recall.keyword_baseline import (  # noqa: E402
    fractional_recall_at_k,
)
from scripts.eval.fre435_memory_recall.onnx_reranker import (  # noqa: E402
    DEFAULT_QWEN_INSTRUCTION,
    OnnxArm,
)
from scripts.eval.fre435_memory_recall.probes import ProbeCase, load_probe_set  # noqa: E402
from scripts.eval.fre435_memory_recall.separation_report import (  # noqa: E402
    SeparationStats,
    best_separation_at_observed,
    percentile,
    summarize_separation,
    truncate_renormalize,
)

DEFAULT_PROBE = "scripts/eval/fre435_memory_recall/semantic_probe.yaml"
DEFAULT_OUT = "telemetry/evaluation/fre435-memory-recall"
_VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
_RECALL_K = (1, 5)

#: Per-arm native dimensionality + the Matryoshka sweep (common 256/512/1024 + native).
ARMS: dict[str, dict[str, object]] = {
    "0.6b": {"native": 1024, "dims": [256, 512, 1024], "kind": "local"},
    "4b-f16": {"native": 2560, "dims": [256, 512, 1024, 2048, 2560], "kind": "local"},
    "8b": {"native": 4096, "dims": [256, 512, 1024, 2048, 4096], "kind": "local"},
    "voyage": {"native": 2048, "dims": [256, 512, 1024, 2048], "kind": "voyage"},
    # FRE-695 MLX embedder ladder (slm endpoint, CF-gated) — runtime + quant cross-check vs
    # the llama.cpp embedder ceiling (J 0.42/0.53/0.53). 8bit vs bf16 isolates the quant effect.
    "mlx-emb-0.6b-bf16": {
        "native": 1024,
        "dims": [256, 512, 1024],
        "kind": "mlx",
        "model": "Qwen/Qwen3-Embedding-0.6B-bf16",
    },
    "mlx-emb-0.6b-8bit": {
        "native": 1024,
        "dims": [256, 512, 1024],
        "kind": "mlx",
        "model": "Qwen/Qwen3-Embedding-0.6B-8bit",
    },
    "mlx-emb-4b-8bit": {
        "native": 2560,
        "dims": [256, 512, 1024, 2048, 2560],
        "kind": "mlx",
        "model": "Qwen/Qwen3-Embedding-4B-8bit",
    },
    "mlx-emb-8b-bf16": {
        "native": 4096,
        "dims": [256, 512, 1024, 2048, 4096],
        "kind": "mlx",
        "model": "Qwen/Qwen3-Embedding-8B-bf16",
    },
    "mlx-emb-8b-8bit": {
        "native": 4096,
        "dims": [256, 512, 1024, 2048, 4096],
        "kind": "mlx",
        "model": "Qwen/Qwen3-Embedding-8B-8bit",
    },
}
_VOYAGE_MODEL = "voyage-4-large"
#: Real Mac SLM tunnel base (FRE-895) — same env var the app reads via
#: settings.slm_tunnel_base_url; unset here defaults to an inert placeholder so
#: an unconfigured run fails obviously rather than hitting a fake host silently.
_SLM_TUNNEL_BASE_URL = os.environ.get("AGENT_SLM_TUNNEL_BASE_URL", "https://slm.example.com")
_SLM_TUNNEL_V1_URL = f"{_SLM_TUNNEL_BASE_URL.rstrip('/')}/v1"
_SLM_EMBED_URL = f"{_SLM_TUNNEL_V1_URL}/embeddings"
#: Qwen3-Embedding query instruction prefix (mirrors memory/embeddings.py) — applied
#: client-side for the MLX arms so they match the local arms' asymmetric query mode.
_QWEN_QUERY_PREFIX = "Instruct: Given a query, retrieve relevant entities and passages\nQuery: "


def _entity_text(name: str, description: str) -> str:
    """Production entity-embedding text (service.create_entity): ``"{name}: {description}"``."""
    return f"{name}: {description}".strip()


def _build_corpus(cases: Sequence[ProbeCase]) -> tuple[dict[str, str], list[ProbeCase]]:
    """Build the co-resident note corpus (first-write-wins per entity name).

    Mirrors the calibrate co-seed: a repeated entity name keeps its FIRST description
    (first-write-wins), so the offline corpus matches the Neo4j one for parity.

    Returns:
        ``(note_text_by_entity, cases)`` — ``note_text_by_entity`` maps the lowercased
        entity name to its embedding text.
    """
    notes: dict[str, str] = {}
    for case in cases:
        for entity in case.seed_entities:
            key = entity.name.strip().lower()
            if key not in notes:
                notes[key] = _entity_text(entity.name, entity.description or "")
    return notes, list(cases)


def _assert_vectors(vectors: Sequence[Sequence[float]], native: int, label: str) -> None:
    """Fail loud on a wrong-length or zero (degenerate) embedding — never score silently."""
    for index, vec in enumerate(vectors):
        if len(vec) != native:
            raise SystemExit(
                f"[fail-loud] {label} vector {index} length {len(vec)} != native {native} "
                "(stale AGENT_EMBEDDING_DIMENSIONS leak or wrong arm?)"
            )
        if not any(x != 0.0 for x in vec):
            raise SystemExit(f"[fail-loud] {label} vector {index} is all-zero (failed embedding)")


async def _embed_local(texts: Sequence[str], mode: str) -> list[list[float]]:
    """Embed via the configured local Qwen embedder (lazy import — cloud arm avoids it)."""
    from personal_agent.memory.embeddings import generate_embeddings_batch  # noqa: PLC0415

    return await generate_embeddings_batch(list(texts), mode=mode)  # type: ignore[arg-type]


def _voyage_key() -> str:
    """Read the Voyage API key from the ``pass`` store (never logged / persisted)."""
    out = subprocess.run(
        ["pass", "show", "VOYAGEAI_API_KEY"], capture_output=True, text=True, check=True
    )
    key = out.stdout.splitlines()[0].strip() if out.stdout else ""
    if not key:
        raise SystemExit("[fail-loud] VOYAGEAI_API_KEY empty in `pass`")
    return key


def _embed_voyage(
    texts: Sequence[str], input_type: str, native: int, key: str
) -> list[list[float]]:
    """Embed via the Voyage API at native dim (batched). input_type query|document."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), 64):
        batch = list(texts[start : start + 64])
        body = json.dumps(
            {
                "model": _VOYAGE_MODEL,
                "input": batch,
                "input_type": input_type,
                "output_dimension": native,
            }
        ).encode()
        request = urllib.request.Request(  # noqa: S310 — fixed https Voyage endpoint
            _VOYAGE_URL,
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            payload = json.load(response)
        vectors.extend([float(x) for x in row["embedding"]] for row in payload["data"])
    return vectors


def _embed_mlx(texts: Sequence[str], mode: str, model: str) -> list[list[float]]:
    """Embed via an MLX embedder on the slm endpoint (CF-gated), native dim, batched.

    The Qwen query instruction prefix is applied client-side (``mode == "query"``) so
    the MLX arm matches the local arms' asymmetric query mode; documents go as-is.
    """
    from personal_agent.service.cf_service_token import (  # noqa: PLC0415
        cf_access_service_token_headers,
    )

    headers = dict(cf_access_service_token_headers())
    headers["User-Agent"] = "seshat-memory/1.0"
    headers["Content-Type"] = "application/json"
    prefix = _QWEN_QUERY_PREFIX if mode == "query" else ""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), 32):
        batch = [f"{prefix}{t}" for t in texts[start : start + 32]]
        body = json.dumps({"model": model, "input": batch}).encode()
        request = urllib.request.Request(  # noqa: S310 — fixed https slm endpoint
            _SLM_EMBED_URL, data=body, headers=headers
        )
        with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310
            payload = json.load(response)
        vectors.extend([float(x) for x in row["embedding"]] for row in payload["data"])
    return vectors


def _embed_arm(
    arm: str, note_texts: Sequence[str], query_texts: Sequence[str], native: int
) -> tuple[list[list[float]], list[list[float]]]:
    """Embed notes (document) + queries (query) for an arm at native dim, fail-loud."""
    if ARMS[arm]["kind"] == "voyage":
        key = _voyage_key()
        notes = _embed_voyage(note_texts, "document", native, key)
        queries = _embed_voyage(query_texts, "query", native, key)
    elif ARMS[arm]["kind"] == "mlx":
        model = str(ARMS[arm]["model"])
        notes = _embed_mlx(note_texts, "document", model)
        queries = _embed_mlx(query_texts, "query", model)
    else:
        notes = asyncio.run(_embed_local(note_texts, "document"))
        queries = asyncio.run(_embed_local(query_texts, "query"))
    _assert_vectors(notes, native, f"{arm} note")
    _assert_vectors(queries, native, f"{arm} query")
    return notes, queries


def _score(a: Sequence[float], b: Sequence[float]) -> float:
    """Neo4j-space similarity score for two unit vectors: ``(cosine + 1) / 2``.

    Neo4j's vector index (``db.index.vector.queryNodes``, the calibrate/production
    path) normalizes cosine from ``[-1, 1]`` to ``[0, 1]``. Reporting offline scores
    in the SAME space keeps them comparable to the FRE-655 floor (0.75) and lets the
    0.6B parity check match the calibrate medians (verified Δ < 0.012). The transform
    is monotonic, so it changes no separation/overlap/ranking verdict — only the scale.
    """
    cosine = sum(x * y for x, y in zip(a, b, strict=True))
    return (cosine + 1.0) / 2.0


#: FRE-670 0.6B calibrate medians (the Neo4j path) — the parity reference (HARD GATE).
#: calibrate aggregated PER CASE: pos = max cosine to an expected entity; neg = max
#: cosine to a non-expected entity. The offline harness must reproduce these at 1024.
_FRE670_CALIBRATE_06B = {"pos_median": 0.7756, "neg_median": 0.7062, "neg_max": 0.7918}
_PARITY_TOL = 0.02


def _parity_check(
    cases: Sequence[ProbeCase],
    note_names: Sequence[str],
    note_vecs_native: Sequence[Sequence[float]],
    query_vecs_native: Sequence[Sequence[float]],
    native: int,
) -> int:
    """Validate offline cosines against the FRE-670 Neo4j calibrate medians (HARD GATE).

    Reproduces calibrate's *per-case max* aggregation at native dim and compares the
    positive/negative medians + neg max to the FRE-670 0.6B reference. A mismatch means
    the offline harness is NOT computing the same geometry as the production path — stop
    and reconcile before trusting any cross-arm number (owner directive, codex axis 1).

    Returns:
        Process exit code (0 = parity holds, 1 = mismatch).
    """
    notes = [truncate_renormalize(v, native) for v in note_vecs_native]
    queries = [truncate_renormalize(v, native) for v in query_vecs_native]
    pos: list[float] = []
    neg: list[float] = []
    for case, qvec in zip(cases, queries, strict=True):
        expected = {n.strip().lower() for n in case.expected.entity_names if n.strip()}
        sims = [(_score(qvec, nvec), name) for name, nvec in zip(note_names, notes, strict=True)]
        if expected:
            pos.append(max(s for s, name in sims if name in expected))
        neg.append(max(s for s, name in sims if name not in expected))
    observed = {
        "pos_median": percentile(pos, 50),
        "neg_median": percentile(neg, 50),
        "neg_max": max(neg),
    }
    print("=== FRE-694 PARITY CHECK — 0.6B@1024 offline vs FRE-670 calibrate (Neo4j) ===")
    ok = True
    for metric, reference in _FRE670_CALIBRATE_06B.items():
        got = observed[metric]
        delta = abs(got - reference)
        within = delta <= _PARITY_TOL
        ok = ok and within
        print(
            f"  {metric:11s} offline={got:.4f}  calibrate={reference:.4f}  "
            f"Δ={delta:.4f}  {'OK' if within else 'MISMATCH'}"
        )
    if ok:
        print(
            f"PARITY HOLDS (all Δ ≤ {_PARITY_TOL}) — offline harness validated; cross-arm numbers trustworthy."
        )
        return 0
    print(
        f"PARITY FAILED (Δ > {_PARITY_TOL}) — STOP and reconcile; do NOT report cross-arm numbers."
    )
    return 1


# ── FRE-695 reranker (cross-encoder) helpers ──────────────────────────────────


def parse_rerank_response(payload: dict[str, object], n_documents: int) -> list[float]:
    """Re-align a /v1/rerank response to input-document order (mirrors reranker.py).

    Handles both wire shapes: llama.cpp returns ``results[]``, Voyage returns ``data[]``
    (an intentional delta — reranker.py reads only ``results[]``). Each item carries an
    ``index`` (into the input ``documents``) and a ``relevance_score``.

    Args:
        payload: The decoded JSON response.
        n_documents: The number of documents sent (the expected result count).

    Returns:
        Relevance scores in the original input order.

    Raises:
        ValueError: If the response is truncated (fewer results than documents) — never
            score a partial candidate set.
    """
    items = payload.get("results")
    if items is None:
        items = payload.get("data")
    items = items or []
    if not isinstance(items, list) or len(items) != n_documents:
        raise ValueError(
            f"truncated rerank response: {len(items) if isinstance(items, list) else 'n/a'} "
            f"results != {n_documents} documents"
        )
    scores = [0.0] * n_documents
    for item in items:
        scores[int(item["index"])] = float(item["relevance_score"])  # type: ignore[index,call-overload]
    return scores


def separation_from_scores(
    cases: Sequence[ProbeCase],
    note_names: Sequence[str],
    score_rows: Sequence[Sequence[float]],
) -> tuple[list[float], list[float]]:
    """Extract positive/negative samples from a (query × note) score matrix.

    Same metric as the embedder path (FRE-694): positives are *per expected entity* (a
    compound case contributes one sample per expected note); negatives are each query's
    strongest *non-expected* note score (positive cases and controls alike).

    Args:
        cases: Probe cases, aligned with ``score_rows``.
        note_names: Lowercased entity keys, aligned with each row's columns.
        score_rows: ``score_rows[i][j]`` = relevance score of query *i* vs ``note_names[j]``.

    Returns:
        ``(positives, negatives)``.
    """
    note_idx = {name: j for j, name in enumerate(note_names)}
    positives: list[float] = []
    negatives: list[float] = []
    for case, row in zip(cases, score_rows, strict=True):
        expected = {n.strip().lower() for n in case.expected.entity_names if n.strip()}
        if expected:
            positives.extend(row[note_idx[name]] for name in expected)
            negatives.append(
                max(
                    (s for name, s in zip(note_names, row, strict=True) if name not in expected),
                    default=0.0,
                )
            )
        else:  # control — every note is a non-match
            negatives.append(max(row, default=0.0))
    return positives, negatives


def positives_negatives_for_case(
    expected: set[str], cand_names: Sequence[str], scores: Sequence[float]
) -> tuple[list[float], float]:
    """Per-case positive/negative reranker samples over one query's candidate scores.

    One definition shared by every reranker arm (HTTP + ONNX) so the benchmark semantics cannot
    drift: positives are *per expected entity* (a compound case contributes one sample per expected
    note actually present in the candidate set — an expected note the shortlist missed contributes
    none, never an invented score); the negative is the query's strongest *non-expected* candidate
    (for a control, where every candidate is a non-match, the strongest candidate overall).

    Args:
        expected: Lowercased expected entity names for the case (empty for a control).
        cand_names: Lowercased candidate note names, aligned with ``scores``.
        scores: Relevance scores, aligned with ``cand_names``.

    Returns:
        ``(positives, negative)`` — the per-expected-entity positive scores and the single strongest
        non-expected (control: overall) score.
    """
    by_name = dict(zip(cand_names, scores, strict=True))
    if expected:
        positives = [by_name[n] for n in expected if n in by_name]
        negative = max((s for n, s in by_name.items() if n not in expected), default=0.0)
        return positives, negative
    return [], max(scores, default=0.0)


def _score_dim(
    cases: Sequence[ProbeCase],
    note_names: Sequence[str],
    note_vecs_native: Sequence[Sequence[float]],
    query_vecs_native: Sequence[Sequence[float]],
    dim: int,
) -> dict[str, object]:
    """Score separation + recall at one reduced dimension.

    Positives are per-expected-entity (a compound case contributes one sample per
    expected entity); negatives are each query's strongest non-expected note (positives
    and controls). Returns a JSON-friendly dict.
    """
    note_idx = {name: i for i, name in enumerate(note_names)}
    notes = [truncate_renormalize(v, dim) for v in note_vecs_native]
    queries = [truncate_renormalize(v, dim) for v in query_vecs_native]

    positives: list[float] = []
    negatives: list[float] = []
    recall_acc: dict[int, list[float]] = {k: [] for k in _RECALL_K}
    for case, qvec in zip(cases, queries, strict=True):
        expected = {n.strip().lower() for n in case.expected.entity_names if n.strip()}
        sims = [(_score(qvec, nvec), name) for name, nvec in zip(note_names, notes, strict=True)]
        if expected:
            for name in expected:
                positives.append(_score(qvec, notes[note_idx[name]]))
            top_non_match = max((s for s, name in sims if name not in expected), default=0.0)
            negatives.append(top_non_match)
            ranked = [name for _, name in sorted(sims, key=lambda p: (-p[0], p[1]))]
            for k in _RECALL_K:
                recall_acc[k].append(fractional_recall_at_k(ranked, expected, k))
        else:  # control — every note is a non-match; its top cosine is over-recall pressure
            negatives.append(max((s for s, _ in sims), default=0.0))

    stats: SeparationStats = summarize_separation(positives, negatives)
    proposal = propose_floor(positives, negatives)
    sweep = sweep_floor(positives, negatives)
    recall = {k: (sum(v) / len(v) if v else 0.0) for k, v in recall_acc.items()}
    return {
        "dim": dim,
        "separation": stats.__dict__,
        "recall": recall,
        "proposed_floor": {
            "floor": proposal.floor,
            "recall": proposal.recall,
            "fpr": proposal.false_positive_rate,
            "youden_j": proposal.youden_j,
        },
        "sweep": [
            {"floor": p.floor, "recall": p.recall, "fpr": p.false_positive_rate} for p in sweep
        ],
    }


def _print_dim(arm: str, row: dict[str, object]) -> None:
    """Print one (arm, dim) separation line."""
    s = row["separation"]
    assert isinstance(s, dict)
    verdict = "CLEAN floor" if s["clean_floor"] else "OVERLAP"
    robust = "robust-clean" if s["robust_clean"] else "robust-overlap"
    recall = row["recall"]
    assert isinstance(recall, dict)
    print(
        f"  {arm:>6} d={row['dim']:>4}  "
        f"pos[min/med/p5={s['pos_min']:.3f}/{s['pos_median']:.3f}/{s['pos_p5']:.3f}]  "
        f"neg[max/med/p95={s['neg_max']:.3f}/{s['neg_median']:.3f}/{s['neg_p95']:.3f}]  "
        f"overlap[neg≥minpos={s['neg_above_min_pos']},pos≤maxneg={s['pos_below_max_neg']}]  "
        f"R@5={recall.get(5, 0.0):.3f}  → {verdict} / {robust}"
    )


# ── FRE-695 reranker (cross-encoder) arms ─────────────────────────────────────
#: Each arm = (rerank endpoint, model id, auth scheme, engine label). The local arms
#: route by model id on the slm gateway; the CPU arm is the same 0.6B-f16 on the VPS.
RERANKER_ARMS: dict[str, dict[str, str]] = {
    "rr-0.6b-gpu": {
        "endpoint": _SLM_TUNNEL_V1_URL,
        "model": "Voodisss/Qwen3-Reranker-0.6B",
        "auth": "cf",
        "engine": "llama.cpp GPU f16",
    },
    "rr-4b-gpu": {
        "endpoint": _SLM_TUNNEL_V1_URL,
        "model": "Voodisss/Qwen3-Reranker-4B",
        "auth": "cf",
        "engine": "llama.cpp GPU f16 (LIVE prod reranker)",
    },
    "rr-0.6b-cpu": {
        "endpoint": "http://localhost:8504/v1",
        "model": "/models/reranker/Qwen3-Reranker-0.6B.F16.gguf",
        "auth": "none",
        "engine": "llama.cpp CPU f16 (VPS cross-check)",
    },
    "rr-mlx-4b": {
        "endpoint": _SLM_TUNNEL_V1_URL,
        "model": "mlx-community/Qwen3-Reranker-4B-mxfp8",
        "auth": "cf",
        "engine": "MLX 4B mxfp8 (port 8508)",
    },
    "rr-mlx-8b": {
        "endpoint": _SLM_TUNNEL_V1_URL,
        "model": "Qwen/Qwen3-Reranker-8B-mxfp8",
        "auth": "cf",
        "engine": "MLX 8B mxfp8 (port 8509)",
    },
    "rr-mlx-8b-bf16": {
        "endpoint": _SLM_TUNNEL_V1_URL,
        "model": "Qwen/Qwen3-Reranker-8B-bf16",
        "auth": "cf",
        "engine": "MLX 8B bf16 (port 8510)",
    },
    "voyage-rerank-2.5": {
        "endpoint": "https://api.voyageai.com/v1",
        "model": "rerank-2.5",
        "auth": "voyage",
        "engine": "Voyage cloud",
    },
    "voyage-rerank-2.5-lite": {
        "endpoint": "https://api.voyageai.com/v1",
        "model": "rerank-2.5-lite",
        "auth": "voyage",
        "engine": "Voyage cloud (lite)",
    },
}


# ── FRE-697 ONNX reranker (in-process, VPS CPU) arms ──────────────────────────
#: The always-on private path: an ONNX cross-encoder scored in-process on the VPS CPU (no laptop, no
#: cloud, no llama.cpp causal-rerank stall). Revisions are pinned commit shas for reproducibility. bge
#: ships a ready int8 export; the Qwen3 seq-cls arm self-quantizes the fp32 export to int8 at load, with
#: an fp32 control for the quant-equivalence gate.
ONNX_RERANKER_ARMS: dict[str, OnnxArm] = {
    "onnx-bge-int8": OnnxArm(
        name="onnx-bge-int8",
        repo="onnx-community/bge-reranker-v2-m3-ONNX",
        revision="6f5ff65298512715a1e669753bc754d2bc8f367b",
        onnx_file="onnx/model_int8.onnx",
        family="bge",
        quantize=False,
        precision="int8 (pre-exported)",
        instruction="",
        engine="bge-reranker-v2-m3 ONNX int8 (VPS CPU)",
        max_length=512,
    ),
    "onnx-qwen-seqcls-fp16": OnnxArm(
        name="onnx-qwen-seqcls-fp16",
        repo="shawnw3i/Qwen3-Reranker-0.6B-seq-cls-ONNX",
        revision="e5d273d8d9fbbc0dc5021008e0242d3cd85bb60d",
        onnx_file="model.onnx",
        family="qwen-seqcls",
        quantize=False,
        precision="fp16 (as published)",
        instruction=DEFAULT_QWEN_INSTRUCTION,
        engine="Qwen3-Reranker-0.6B seq-cls ONNX fp16 (VPS CPU)",
        max_length=1024,
    ),
    "onnx-qwen-seqcls-int8": OnnxArm(
        name="onnx-qwen-seqcls-int8",
        repo="shawnw3i/Qwen3-Reranker-0.6B-seq-cls-ONNX",
        revision="e5d273d8d9fbbc0dc5021008e0242d3cd85bb60d",
        onnx_file="model.onnx",
        family="qwen-seqcls",
        quantize=True,
        precision="int8-dynamic",
        instruction=DEFAULT_QWEN_INSTRUCTION,
        engine="Qwen3-Reranker-0.6B seq-cls ONNX int8-dynamic (VPS CPU)",
        max_length=1024,
    ),
}


#: Easy relevant/irrelevant pair for the per-arm instrument-sanity gate (rank-order).
class _RerankStalled(Exception):
    """A reranker backend stalled (5xx/timeout) — caught to keep partial results."""


_SANITY_QUERY = (
    "that faint leftover glow from when the early universe first cooled enough to turn clear"
)
_SANITY_RELEVANT = (
    "cosmic microwave background: relic radiation left over from recombination, roughly 380,000 "
    "years after the Big Bang"
)
_SANITY_IRRELEVANT = (
    "ratatouille: a Provencal stew of aubergine, courgette, pepper and tomato cooked separately"
)


def _rerank_headers(arm_meta: dict[str, str]) -> dict[str, str]:
    """Auth headers for an arm: CF-Access for slm, bearer for Voyage, none for the CPU arm."""
    auth = arm_meta["auth"]
    if auth == "cf":
        from personal_agent.service.cf_service_token import (  # noqa: PLC0415
            cf_access_service_token_headers,
        )

        headers = dict(cf_access_service_token_headers())
        headers["User-Agent"] = "seshat-memory/1.0"
        return headers
    if auth == "voyage":
        return {"Authorization": f"Bearer {_voyage_key()}", "Content-Type": "application/json"}
    return {}


async def _rerank(
    client: httpx.AsyncClient, arm_meta: dict[str, str], query: str, documents: Sequence[str]
) -> list[float]:
    """One /v1/rerank request scoring ALL documents at once (mirrors reranker.py).

    Scores the full candidate set in a single request (``top_n = len(documents)``) — never
    per-document or chunked — so the relevance score reflects the same listwise candidate
    context production uses. Retries transient 429/5xx with backoff. Returns scores in the
    input document order.
    """
    payload: dict[str, object] = {
        "model": arm_meta["model"],
        "query": query,
        "documents": list(documents),
    }
    if arm_meta["auth"] != "voyage":
        # llama.cpp truncates to top_n (we want all scored); Voyage rejects top_n
        # (it uses top_k; omitting returns every document ranked).
        payload["top_n"] = len(documents)
    # Retry only true RATE limits (429 — Voyage). A 5xx/504 here means the local
    # backend wedged (llama.cpp #17200 KV stall); re-sending the SAME request just
    # piles onto the stuck slot, so fail FAST and loud — never hammer with the repeat.
    last_err: str = "no attempts"
    for attempt in range(3):
        try:
            resp = await client.post(f"{arm_meta['endpoint']}/rerank", json=payload)
            if resp.status_code == 429:
                last_err = "HTTP 429 (rate limit)"
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            if resp.status_code >= 500:  # backend stall — do NOT retry the same request
                raise _RerankStalled(
                    f"backend {resp.status_code} (llama.cpp #17200 KV-stall); not retrying"
                )
            resp.raise_for_status()
            return parse_rerank_response(resp.json(), len(documents))
        except httpx.TimeoutException:
            raise _RerankStalled(f"backend timeout ({arm_meta['model']}); wedged") from None
        except httpx.HTTPError as exc:
            last_err = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
            await asyncio.sleep(1.0 * (attempt + 1))
    raise _RerankStalled(f"rerank failed ({arm_meta['model']}): {last_err}")


async def _sanity_check(
    client: httpx.AsyncClient, arm_meta: dict[str, str]
) -> tuple[bool, float, float, float]:
    """Rank-order sanity: the relevant doc must rank #1 and out-score the irrelevant one.

    Scale-agnostic (no fixed 0.5 threshold — score scales differ across rerankers). Returns
    ``(ok, relevant_score, irrelevant_score, gap)``; the gap is recorded as the arm's local
    calibration reference.
    """
    scores = await _rerank(client, arm_meta, _SANITY_QUERY, [_SANITY_RELEVANT, _SANITY_IRRELEVANT])
    relevant, irrelevant = scores[0], scores[1]
    gap = relevant - irrelevant
    return (relevant > irrelevant, relevant, irrelevant, gap)


async def _chunk_check(
    client: httpx.AsyncClient,
    arm_meta: dict[str, str],
    cases: Sequence[ProbeCase],
    documents: Sequence[str],
    note_names: Sequence[str],
) -> dict[str, float]:
    """Listwise-normalization probe: does an expected note's score change with batch size?

    Re-scores one positive query against (a) the full 49-doc set and (b) just its expected
    note + the strongest distractor. If the expected note's score moves materially, the
    reranker normalizes listwise (so the full-set scoring discipline matters); if stable,
    scoring is pairwise-independent. Diagnostic only.
    """
    case = next(c for c in cases if c.expected.entity_names)
    expected = case.expected.entity_names[0].strip().lower()
    note_idx = {n: i for i, n in enumerate(note_names)}
    full = await _rerank(client, arm_meta, case.query, documents)
    expected_full = full[note_idx[expected]]
    # strongest non-expected note from the full pass
    distractor_j = max(
        (i for i, n in enumerate(note_names) if n != expected), key=lambda i: full[i]
    )
    pair = await _rerank(
        client, arm_meta, case.query, [documents[note_idx[expected]], documents[distractor_j]]
    )
    return {
        "full_set": round(expected_full, 4),
        "pair": round(pair[0], 4),
        "delta": round(abs(expected_full - pair[0]), 4),
    }


async def _embedder_shortlist(
    note_names: Sequence[str],
    note_texts: Sequence[str],
    query_texts: Sequence[str],
    top_n: int,
) -> list[list[int]]:
    """Per-query top-n note indices by the 0.6B production embedder (the rerank candidate set).

    Production retrieves with the embedder then reranks its shortlist — so the reranker
    benchmark reranks the embedder's top-n (plus the case's expected notes, added by the
    caller), not the whole 49-note corpus. This keeps each rerank request small (well
    inside the 8192-token reranker context) and faithful to production. Uses the VPS
    embedder (:8503) — zero laptop-GPU load.
    """
    from personal_agent.memory.embeddings import generate_embeddings_batch  # noqa: PLC0415

    note_vecs = await generate_embeddings_batch(list(note_texts), mode="document")
    query_vecs = await generate_embeddings_batch(list(query_texts), mode="query")

    def _unit(vec: Sequence[float]) -> list[float]:
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        return [x / norm for x in vec]

    units = [_unit(v) for v in note_vecs]
    shortlists: list[list[int]] = []
    for query_vec in query_vecs:
        qu = _unit(query_vec)
        order = sorted(
            range(len(note_names)),
            key=lambda j: -sum(a * b for a, b in zip(qu, units[j], strict=True)),
        )
        shortlists.append(order[:top_n])
    return shortlists


async def _run_reranker(arm: str, args: argparse.Namespace) -> int:
    """Score the probe through a reranker arm; report separation on relevance scores."""
    arm_meta = RERANKER_ARMS[arm]
    cases = load_probe_set(Path(args.probe))
    notes_by_entity, cases = _build_corpus(cases)
    note_names = list(notes_by_entity)
    documents = [notes_by_entity[name] for name in note_names]

    async with httpx.AsyncClient(timeout=240.0, headers=_rerank_headers(arm_meta)) as client:
        try:
            ok, relevant, irrelevant, gap = await _sanity_check(client, arm_meta)
        except _RerankStalled as exc:
            raise SystemExit(f"[fail-loud] sanity stalled for {arm}: {exc}") from None
        print(f"=== FRE-695 reranker — arm={arm} ({arm_meta['engine']}) ===")
        print(
            f"sanity: relevant={relevant:.4f} irrelevant={irrelevant:.4f} gap={gap:.4f} "
            f"-> {'OK (relevant ranks #1)' if ok else 'FAIL'}"
        )
        if not ok:
            raise SystemExit(
                f"[fail-loud] sanity failed for {arm}: relevant must out-score irrelevant"
            )
        if args.chunk_check:
            chunk = await _chunk_check(client, arm_meta, cases, documents, note_names)
            print(f"chunk-check (listwise?): {json.dumps(chunk)}")
        if args.sanity:
            return 0
        # Rerank a per-query candidate set, not the whole corpus: production reranks
        # the embedder's shortlist (~top-N), and small requests stay well inside the
        # reranker's context window. --candidates 0 reranks all notes (legacy).
        shortlists: list[list[int]] | None = None
        if args.candidates and args.candidates > 0:
            shortlists = await _embedder_shortlist(
                note_names, documents, [c.query for c in cases], args.candidates
            )
        note_index = {name: j for j, name in enumerate(note_names)}
        positives: list[float] = []
        negatives: list[float] = []
        latencies_ms: list[float] = []
        cand_sizes: list[int] = []
        stalled_at: int | None = None
        pause_s = max(0.0, args.pause_ms / 1000.0)
        for index, case in enumerate(cases):
            if index and pause_s:
                await asyncio.sleep(pause_s)  # breathing room for the shared backend
            expected = {n.strip().lower() for n in case.expected.entity_names if n.strip()}
            if shortlists is not None:
                # embedder top-N ∪ the case's expected notes (so positives are scored)
                cand_idx = list(
                    dict.fromkeys(
                        [*shortlists[index], *(note_index[n] for n in expected if n in note_index)]
                    )
                )
            else:
                cand_idx = list(range(len(note_names)))
            cand_names = [note_names[j] for j in cand_idx]
            cand_docs = [documents[j] for j in cand_idx]
            t0 = time.monotonic()
            try:
                scores = await _rerank(client, arm_meta, case.query, cand_docs)
            except _RerankStalled as exc:
                stalled_at = index  # keep what we have up to the wedge
                print(f"  ⚠️ STALLED at query {index + 1}/{len(cases)} ({case.case_id}): {exc}")
                break
            latencies_ms.append((time.monotonic() - t0) * 1000.0)
            cand_sizes.append(len(cand_docs))
            case_pos, case_neg = positives_negatives_for_case(expected, cand_names, scores)
            positives.extend(case_pos)
            negatives.append(case_neg)
    completed = len(latencies_ms)
    if stalled_at is not None:
        print(
            f"  PARTIAL: backend wedged after {completed}/{len(cases)} queries "
            f"(llama.cpp #17200) — reporting separation on what completed."
        )
    if len(positives) < 3 or len(negatives) < 3:
        raise SystemExit(
            f"[partial] only {completed} queries before the wedge "
            f"({len(positives)} pos / {len(negatives)} neg) — too few to score separation."
        )
    warm = latencies_ms[1:] or latencies_ms  # drop the cold first (model-load) call
    stats = summarize_separation(positives, negatives)
    best = best_separation_at_observed(positives, negatives)
    latency = {
        "candidates_per_query": round(sum(cand_sizes) / len(cand_sizes), 1),
        "warm_median_ms": round(percentile(warm, 50), 1),
        "warm_p95_ms": round(percentile(warm, 95), 1),
        "cold_first_ms": round(latencies_ms[0], 1),  # model lazy-load on the shared GPU
    }
    run_record = {
        "arm": arm,
        "engine": arm_meta["engine"],
        "model": arm_meta["model"],
        "n_notes": len(note_names),
        "n_queries": len(cases),
        "completed_queries": completed,
        "partial": stalled_at is not None,
        "candidates": args.candidates,
        "probe": args.probe,
        "sanity_gap": round(gap, 4),
        "latency": latency,
    }
    print(f"run-record: {json.dumps(run_record)}")
    print(
        f"  latency (rerank ~{latency['candidates_per_query']} candidates/query): "
        f"warm-median={latency['warm_median_ms']}ms warm-p95={latency['warm_p95_ms']}ms "
        f"cold-first={latency['cold_first_ms']}ms"
    )
    verdict = "CLEAN floor" if stats.clean_floor else "OVERLAP"
    robust = "robust-clean" if stats.robust_clean else "robust-overlap"
    print(
        f"  {arm}: pos[min/med/p5={stats.pos_min:.3f}/{stats.pos_median:.3f}/{stats.pos_p5:.3f}]  "
        f"neg[max/med/p95={stats.neg_max:.3f}/{stats.neg_median:.3f}/{stats.neg_p95:.3f}]  "
        f"overlap[neg≥minpos={stats.neg_above_min_pos},pos≤maxneg={stats.pos_below_max_neg}]  "
        f"bestJ={best.youden_j:.3f}@{best.floor:.4f}(R{best.recall:.2f}/FP{best.false_positive_rate:.2f})  "
        f"-> {verdict} / {robust}"
    )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"separation-{arm}.json"
    out_path.write_text(
        json.dumps(
            {
                "run_record": run_record,
                "separation": stats.__dict__,
                "best_floor": {
                    "floor": best.floor,
                    "recall": best.recall,
                    "fpr": best.false_positive_rate,
                    "youden_j": best.youden_j,
                },
            },
            indent=2,
        )
    )
    print(f"written: {out_path}  (gitignored — commit only curated aggregates)")
    return 0


async def _run_onnx_reranker(arm_name: str, args: argparse.Namespace) -> int:
    """Score the probe through an in-process ONNX cross-encoder on the VPS CPU (FRE-697).

    Mirrors :func:`_run_reranker`'s reporting shape but scores in-process (no HTTP endpoint, no laptop,
    no cloud): loads the ONNX arm (thread-bounded CPU session, optional dynamic-int8), runs the
    model-card instrument-verification gate (STOP on fail), reranks each query's production top-N
    shortlist (∪ its expected notes), times each forward pass for CPU latency, and reports separation
    (best Youden's J, overlap, robust p5/p95, clean-floor verdict) plus a provenance block.

    Args:
        arm_name: The ONNX arm key in :data:`ONNX_RERANKER_ARMS`.
        args: Parsed CLI args (``probe``, ``out``, ``candidates``, ``threads``, ``sanity``).

    Returns:
        Process exit code (0 on success).

    Raises:
        SystemExit: On a failed instrument gate, a per-query timeout (runaway guard), or too few
            samples to score separation.
    """
    from scripts.eval.fre435_memory_recall.onnx_reranker import OnnxCrossEncoder  # noqa: PLC0415

    arm = ONNX_RERANKER_ARMS[arm_name]
    cases = load_probe_set(Path(args.probe))
    notes_by_entity, cases = _build_corpus(cases)
    note_names = list(notes_by_entity)
    documents = [notes_by_entity[name] for name in note_names]

    cache_dir = Path(args.out) / "onnx-int8-cache"
    scorer = OnnxCrossEncoder(arm)
    await asyncio.to_thread(scorer.load, cache_dir=cache_dir, intra_op_threads=args.threads)

    ok, true_score, best_distractor = await asyncio.to_thread(scorer.verify_instrument)
    print(f"=== FRE-697 ONNX reranker — arm={arm_name} ({arm.engine}) ===")
    print(
        f"verify: true-match={true_score:.4f} best-distractor={best_distractor:.4f} "
        f"-> {'OK (Mars ranks #1)' if ok else 'FAIL'}"
    )
    if not ok:
        raise SystemExit(
            f"[fail-loud] instrument verification failed for {arm_name}: the true match must "
            "out-rank every distractor (wrong template / tokenizer / logit polarity?)"
        )
    if args.sanity:
        return 0

    shortlists: list[list[int]] | None = None
    if args.candidates and args.candidates > 0:
        shortlists = await _embedder_shortlist(
            note_names, documents, [c.query for c in cases], args.candidates
        )
    note_index = {name: j for j, name in enumerate(note_names)}
    positives: list[float] = []
    negatives: list[float] = []
    latencies_ms: list[float] = []
    cand_sizes: list[int] = []
    query_guard_s = 30.0  # a single CPU forward pass over ~15 docs must not pin the live host
    for index, case in enumerate(cases):
        expected = {n.strip().lower() for n in case.expected.entity_names if n.strip()}
        if shortlists is not None:
            cand_idx = list(
                dict.fromkeys(
                    [*shortlists[index], *(note_index[n] for n in expected if n in note_index)]
                )
            )
        else:
            cand_idx = list(range(len(note_names)))
        cand_names = [note_names[j] for j in cand_idx]
        cand_docs = [documents[j] for j in cand_idx]
        t0 = time.monotonic()
        try:
            scores = await asyncio.wait_for(
                asyncio.to_thread(scorer.score, case.query, cand_docs), timeout=query_guard_s
            )
        except TimeoutError:
            raise SystemExit(
                f"[fail-loud] ONNX score exceeded {query_guard_s:.0f}s at query {index + 1}"
                f"/{len(cases)} ({case.case_id}) — aborting to protect the live host."
            ) from None
        latencies_ms.append((time.monotonic() - t0) * 1000.0)
        cand_sizes.append(len(cand_docs))
        case_pos, case_neg = positives_negatives_for_case(expected, cand_names, scores)
        positives.extend(case_pos)
        negatives.append(case_neg)

    completed = len(latencies_ms)
    if len(positives) < 3 or len(negatives) < 3:
        raise SystemExit(
            f"[partial] only {completed} queries scored "
            f"({len(positives)} pos / {len(negatives)} neg) — too few to score separation."
        )
    warm = latencies_ms[1:] or latencies_ms  # drop the cold first (session warm-up) call
    stats = summarize_separation(positives, negatives)
    best = best_separation_at_observed(positives, negatives)
    latency = {
        "candidates_per_query": round(sum(cand_sizes) / len(cand_sizes), 1),
        "warm_median_ms": round(percentile(warm, 50), 1),
        "warm_p95_ms": round(percentile(warm, 95), 1),
        "cold_first_ms": round(latencies_ms[0], 1),
    }
    run_record = {
        "arm": arm_name,
        "engine": arm.engine,
        "family": arm.family,
        "n_notes": len(note_names),
        "n_queries": len(cases),
        "completed_queries": completed,
        "partial": completed != len(cases),
        "candidates": args.candidates,
        "probe": args.probe,
        "instrument_true_match": round(true_score, 4),
        "instrument_best_distractor": round(best_distractor, 4),
        "provenance": scorer.provenance,
        "latency": latency,
    }
    print(f"run-record: {json.dumps(run_record)}")
    print(
        f"  latency (rerank ~{latency['candidates_per_query']} candidates/query): "
        f"warm-median={latency['warm_median_ms']}ms warm-p95={latency['warm_p95_ms']}ms "
        f"cold-first={latency['cold_first_ms']}ms"
    )
    verdict = "CLEAN floor" if stats.clean_floor else "OVERLAP"
    robust = "robust-clean" if stats.robust_clean else "robust-overlap"
    print(
        f"  {arm_name}: "
        f"pos[min/med/p5={stats.pos_min:.3f}/{stats.pos_median:.3f}/{stats.pos_p5:.3f}]  "
        f"neg[max/med/p95={stats.neg_max:.3f}/{stats.neg_median:.3f}/{stats.neg_p95:.3f}]  "
        f"overlap[neg≥minpos={stats.neg_above_min_pos},pos≤maxneg={stats.pos_below_max_neg}]  "
        f"n[pos={stats.n_positives},neg={stats.n_negatives}]  "
        f"bestJ={best.youden_j:.3f}@{best.floor:.4f}(R{best.recall:.2f}/FP{best.false_positive_rate:.2f})  "
        f"-> {verdict} / {robust}"
    )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"separation-{arm_name}.json"
    out_path.write_text(
        json.dumps(
            {
                "run_record": run_record,
                "separation": stats.__dict__,
                "best_floor": {
                    "floor": best.floor,
                    "recall": best.recall,
                    "fpr": best.false_positive_rate,
                    "youden_j": best.youden_j,
                },
            },
            indent=2,
        )
    )
    print(f"written: {out_path}  (gitignored — commit only curated aggregates)")
    return 0


def run(args: argparse.Namespace) -> int:
    """Embed the probe for one arm, sweep dimensions, write the separation report."""
    if args.arm in ONNX_RERANKER_ARMS:
        return asyncio.run(_run_onnx_reranker(args.arm, args))
    if args.arm in RERANKER_ARMS:
        return asyncio.run(_run_reranker(args.arm, args))
    arm = args.arm
    native = int(ARMS[arm]["native"])
    dims = [d for d in ARMS[arm]["dims"] if not args.dims or d in args.dims]  # type: ignore[union-attr]
    cases = load_probe_set(Path(args.probe))
    notes_by_entity, cases = _build_corpus(cases)
    note_names = list(notes_by_entity)
    note_texts = [notes_by_entity[name] for name in note_names]
    query_texts = [case.query for case in cases]

    note_vecs, query_vecs = _embed_arm(arm, note_texts, query_texts, native)

    if args.parity:
        if arm != "0.6b":
            raise SystemExit("--parity is the 0.6B@native gate; run it with --arm 0.6b")
        return _parity_check(cases, note_names, note_vecs, query_vecs, native)

    run_record = {
        "arm": arm,
        "model": _VOYAGE_MODEL if ARMS[arm]["kind"] == "voyage" else "Qwen3-Embedding (f16)",
        "native_dim": native,
        "dims": dims,
        "reduction": "client-side first-N + L2 renormalize (MRL)",
        "query_mode": "voyage:input_type"
        if ARMS[arm]["kind"] == "voyage"
        else "qwen:instruct-prefix",
        "n_notes": len(note_names),
        "n_queries": len(query_texts),
        "probe": args.probe,
    }
    print(f"=== FRE-694 separation — arm={arm} (native {native}, f16) ===")
    print(f"run-record: {json.dumps(run_record)}")
    rows = [_score_dim(cases, note_names, note_vecs, query_vecs, dim) for dim in dims]
    for row in rows:
        _print_dim(arm, row)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"separation-{arm}.json"
    out_path.write_text(json.dumps({"run_record": run_record, "dims": rows}, indent=2))
    print(f"written: {out_path}  (gitignored — commit only curated aggregates)")
    return 0


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="FRE-694/695 embedder + reranker separation benchmark"
    )
    parser.add_argument(
        "--arm",
        required=True,
        choices=sorted([*ARMS, *RERANKER_ARMS, *ONNX_RERANKER_ARMS]),
        help="Embedder arm (FRE-694), HTTP reranker arm (FRE-695), or ONNX-CPU reranker arm (FRE-697).",
    )
    parser.add_argument("--probe", default=DEFAULT_PROBE, help="Probe YAML path.")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output dir (gitignored).")
    parser.add_argument(
        "--dims",
        type=int,
        nargs="*",
        default=None,
        help="Restrict the sweep to these dims (embedder).",
    )
    parser.add_argument(
        "--parity",
        action="store_true",
        help="HARD GATE (embedder): 0.6B@native vs FRE-670 calibrate medians.",
    )
    parser.add_argument(
        "--sanity",
        action="store_true",
        help="Reranker: run the rank-order instrument-sanity gate only, then stop.",
    )
    parser.add_argument(
        "--chunk-check",
        action="store_true",
        help="Reranker: probe whether the engine normalizes listwise (full-set vs pair score).",
    )
    parser.add_argument(
        "--pause-ms",
        type=float,
        default=250.0,
        help="Reranker: pause between per-query requests (breathing room for the shared GPU).",
    )
    parser.add_argument(
        "--candidates",
        type=int,
        default=15,
        help="Reranker: rerank the 0.6B embedder's top-N candidates per query (0 = all notes). "
        "Small N keeps each request inside the reranker context window + mirrors production.",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=4,
        help="ONNX reranker (FRE-697): onnxruntime intra-op thread cap on the shared VPS "
        "(of 8 cores — leaves headroom for the live gateway).",
    )
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
