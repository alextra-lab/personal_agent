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
import urllib.request  # noqa: E402
from collections.abc import Sequence  # noqa: E402
from pathlib import Path  # noqa: E402

from scripts.eval.fre435_memory_recall.calibration import propose_floor, sweep_floor  # noqa: E402
from scripts.eval.fre435_memory_recall.keyword_baseline import (  # noqa: E402
    fractional_recall_at_k,
)
from scripts.eval.fre435_memory_recall.probes import ProbeCase, load_probe_set  # noqa: E402
from scripts.eval.fre435_memory_recall.separation_report import (  # noqa: E402
    SeparationStats,
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
}
_VOYAGE_MODEL = "voyage-4-large"


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


def _embed_arm(
    arm: str, note_texts: Sequence[str], query_texts: Sequence[str], native: int
) -> tuple[list[list[float]], list[list[float]]]:
    """Embed notes (document) + queries (query) for an arm at native dim, fail-loud."""
    if ARMS[arm]["kind"] == "voyage":
        key = _voyage_key()
        notes = _embed_voyage(note_texts, "document", native, key)
        queries = _embed_voyage(query_texts, "query", native, key)
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


def run(args: argparse.Namespace) -> int:
    """Embed the probe for one arm, sweep dimensions, write the separation report."""
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
    parser = argparse.ArgumentParser(description="FRE-694 embedder separation benchmark")
    parser.add_argument("--arm", required=True, choices=sorted(ARMS), help="Embedder arm.")
    parser.add_argument("--probe", default=DEFAULT_PROBE, help="Probe YAML path.")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output dir (gitignored).")
    parser.add_argument(
        "--dims", type=int, nargs="*", default=None, help="Restrict the sweep to these dims."
    )
    parser.add_argument(
        "--parity",
        action="store_true",
        help="HARD GATE: 0.6B@native vs FRE-670 calibrate medians (run before trusting arms).",
    )
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
