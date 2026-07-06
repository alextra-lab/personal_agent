"""FRE-817 -- ADR-0112 AC-4 corpus A/B: nDCG@k across embedder arms.

Offline, no-substrate pattern (mirrors
``scripts.eval.fre435_memory_recall.separation_benchmark``): embed the fixed
real-query corpus's notes + queries for each arm, rank notes by cosine per
query, score nDCG@k against the corpus's labelled expectations. This answers
"which embedder ranks better on our real queries" -- a different question
from that module's clean-floor *separation* geometry, same harness family.

Known limitation (offline vs. live Neo4j retrieval path): this measures
embedding-geometry ranking quality, not "will the production Neo4j HNSW path
retrieve this." The 0.6B arm reuses the exact construction
(``_entity_text``, same config/dimension) already validated against the
FRE-670 Neo4j calibrate medians by ``separation_benchmark.py``'s
``_parity_check`` (Delta <= 0.02) -- so its offline nDCG numbers inherit that
parity. The OVH-8B arm has no equivalent live-Neo4j reference (there is no
production index at 4096-dim today); the offline geometry comparison is the
measurement AC-4 can practically ask for at this stage. Recorded explicitly
in the research writeup, not assumed away.

Run (owner-authorized one-off exception to ADR-0112 D3's off-host default;
pulls OVH credentials from ``pass`` at run time, never persisted):

    scripts/eval/fre817_corpus_ab_embedder/run_corpus_ab.sh
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx
from scripts.eval.fre435_memory_recall.metrics import mean_optional, ndcg_at_k
from scripts.eval.fre435_memory_recall.probes import ENTITY_NS, ProbeCase, load_probe_set
from scripts.eval.fre817_corpus_ab_embedder.decision import (
    PRE_REGISTERED_MARGIN_NDCG,
    EmbedderCandidate,
    decide_embedder,
)

DEFAULT_PROBE = "scripts/eval/fre435_memory_recall/semantic_probe.yaml"
DEFAULT_OUT = "telemetry/evaluation/fre817-corpus-ab"
_KS = (1, 5)
_DECISION_K = 5

#: Qwen3-Embedding query instruction prefix (mirrors memory/embeddings.py and
#: separation_benchmark.py's MLX-arm precedent) -- applied client-side so the
#: OVH arm matches the local arm's asymmetric query mode.
_QWEN_QUERY_PREFIX = "Instruct: Given a query, retrieve relevant entities and passages\nQuery: "

#: Rank-order instrument-sanity pair (copied from separation_benchmark.py's
#: reranker sanity gate) -- the relevant text must out-score the irrelevant
#: one before any full-corpus spend, catching a wrong model id or a
#: misinterpreted query prefix that would otherwise look like a normal vector.
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


def _unit(vec: Sequence[float]) -> list[float]:
    """L2-normalize a vector (defensive -- some endpoints don't guarantee unit norm)."""
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / norm for x in vec]


def score_arm(
    cases: Sequence[ProbeCase],
    note_names: Sequence[str],
    note_vecs: Sequence[Sequence[float]],
    query_vecs: Sequence[Sequence[float]],
    ks: Sequence[int],
) -> dict[int, float | None]:
    """Pure nDCG@k aggregation for one embedder arm -- no embedding I/O.

    Args:
        cases: Probe cases, aligned 1:1 with ``query_vecs``.
        note_names: Lowercased, stripped entity keys, aligned with ``note_vecs``
            (matches ``ProbeCase.relevant_ids``' normalisation).
        note_vecs: Per-note embedding vectors (need not be pre-normalized).
        query_vecs: Per-case query embedding vectors, aligned with ``cases``.
        ks: The nDCG cut-offs to compute.

    Returns:
        Mapping of ``k`` to the mean nDCG@k over cases with a non-empty
        expected set (``mean_optional`` -- controls contribute ``None`` and
        are excluded, matching ``metrics.py`` convention).
    """
    note_units = [_unit(v) for v in note_vecs]
    per_k: dict[int, list[float | None]] = {k: [] for k in ks}
    for case, query_vec in zip(cases, query_vecs, strict=True):
        query_unit = _unit(query_vec)
        ranked = sorted(
            range(len(note_names)),
            key=lambda i: -sum(a * b for a, b in zip(query_unit, note_units[i], strict=True)),
        )
        retrieved = [f"{ENTITY_NS}{note_names[i]}" for i in ranked]
        relevant = set(case.relevant_ids)
        for k in ks:
            per_k[k].append(ndcg_at_k(retrieved, relevant, k))
    return {k: mean_optional(values) for k, values in per_k.items()}


def _entity_text(name: str, description: str) -> str:
    """Production entity-embedding text (matches ``service.create_entity``)."""
    return f"{name}: {description}".strip()


def _build_corpus(cases: Sequence[ProbeCase]) -> tuple[dict[str, str], list[ProbeCase]]:
    """Build the co-resident note corpus (first-write-wins per entity name).

    Mirrors ``separation_benchmark.py``'s ``_build_corpus`` so the two
    harnesses stay consistent, without reaching into that module's private
    helper.
    """
    notes: dict[str, str] = {}
    for case in cases:
        for entity in case.seed_entities:
            key = entity.name.strip().lower()
            if key not in notes:
                notes[key] = _entity_text(entity.name, entity.description or "")
    return notes, list(cases)


def _assert_vectors(vectors: Sequence[Sequence[float]], expected_dim: int, label: str) -> None:
    """Fail loud on a wrong-length or zero (degenerate) embedding -- never score silently."""
    for index, vec in enumerate(vectors):
        if len(vec) != expected_dim:
            raise SystemExit(
                f"[fail-loud] {label} vector {index} length {len(vec)} != expected {expected_dim}"
            )
        if not any(x != 0.0 for x in vec):
            raise SystemExit(f"[fail-loud] {label} vector {index} is all-zero (failed embedding)")


async def _embed_local(texts: Sequence[str], mode: str) -> list[list[float]]:
    """Embed via the configured local (currently-deployed 0.6B) Qwen embedder."""
    from personal_agent.memory.embeddings import generate_embeddings_batch  # noqa: PLC0415

    return await generate_embeddings_batch(list(texts), mode=mode)  # type: ignore[arg-type]


def _ovh_credentials() -> tuple[str, str]:
    """Read the OVH AI Endpoints base URL + token from ``pass`` (never logged/persisted)."""

    def _read(entry: str) -> str:
        out = subprocess.run(["pass", "show", entry], capture_output=True, text=True, check=True)
        value = out.stdout.splitlines()[0].strip() if out.stdout else ""
        if not value:
            raise SystemExit(f"[fail-loud] `pass show {entry}` returned empty")
        return value

    return _read("seshat/AGENT_OVH_AI_BASE_URL"), _read("seshat/AGENT_OVH_EMBEDDING_TOKEN")


#: OVH's Qwen3-Embedding-8B endpoint rejects a batch bigger than this with
#: HTTP 400 ("given batch size overflow maximal one", max=25) -- confirmed
#: live against the endpoint (2026-07-06). ``_embed_ovh`` chunks to this size.
_OVH_MAX_BATCH = 25


async def _embed_ovh_batch(
    texts: Sequence[str],
    mode: str,
    base_url: str,
    token: str,
    model: str,
    client: httpx.AsyncClient,
) -> list[list[float]]:
    """One OVH embeddings request for a batch within the endpoint's size limit.

    Fail-loud on cardinality mismatch and re-sorts by each row's ``index``
    field before extraction -- the response is never trusted to preserve
    input order (mirrors the reranker path's ``parse_rerank_response``
    truncation guard, applied here to the embeddings response).

    Raises:
        SystemExit: On a response whose row count doesn't match the batch size.
    """
    prefix = _QWEN_QUERY_PREFIX if mode == "query" else ""
    payload = {"model": model, "input": [f"{prefix}{t}" for t in texts]}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"{base_url.rstrip('/')}/embeddings"
    response = await client.post(url, json=payload, headers=headers)
    response.raise_for_status()
    rows = response.json()["data"]
    if len(rows) != len(texts):
        raise SystemExit(
            f"[fail-loud] OVH embeddings response returned {len(rows)} rows for "
            f"{len(texts)} inputs -- truncated/expanded response, refusing to score"
        )
    ordered = sorted(rows, key=lambda row: row["index"])
    return [[float(x) for x in row["embedding"]] for row in ordered]


async def _embed_ovh(
    texts: Sequence[str],
    mode: str,
    base_url: str,
    token: str,
    model: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[list[float]]:
    """Embed via the OVH AI Endpoints managed Qwen3-Embedding-8B (OpenAI-compatible API).

    Chunks ``texts`` to :data:`_OVH_MAX_BATCH` per request (the endpoint
    rejects larger batches) and concatenates results in input order.

    Args:
        texts: Input texts to embed.
        mode: ``"query"`` (prepends the Qwen instruction prefix) or ``"document"``.
        base_url: OVH AI Endpoints base URL (e.g. ``https://<model>.endpoints...``).
        token: Bearer token.
        model: The model id to request.
        client: Injected client for testing (a real one is opened when omitted).

    Returns:
        Embedding vectors in input order.

    Raises:
        SystemExit: On a non-2xx response or a response whose row count
            doesn't match the input count.
    """
    chunks = [
        texts[start : start + _OVH_MAX_BATCH] for start in range(0, len(texts), _OVH_MAX_BATCH)
    ]
    if client is not None:
        results = [
            await _embed_ovh_batch(chunk, mode, base_url, token, model, client) for chunk in chunks
        ]
    else:
        async with httpx.AsyncClient(timeout=120.0) as owned_client:
            results = [
                await _embed_ovh_batch(chunk, mode, base_url, token, model, owned_client)
                for chunk in chunks
            ]
    return [vec for batch in results for vec in batch]


async def _sanity_check_ovh(base_url: str, token: str, model: str) -> None:
    """Rank-order sanity: the relevant text must out-cosine the irrelevant one.

    Catches a wrong model id or a query prefix the endpoint doesn't interpret
    as intended -- a content mismatch that length/non-zero checks alone can't
    see, since a wrong-but-valid response still looks like a normal vector.
    """
    query_vec, doc_vecs = await asyncio.gather(
        _embed_ovh([_SANITY_QUERY], "query", base_url, token, model),
        _embed_ovh([_SANITY_RELEVANT, _SANITY_IRRELEVANT], "document", base_url, token, model),
    )
    q = _unit(query_vec[0])
    relevant_cos = sum(a * b for a, b in zip(q, _unit(doc_vecs[0]), strict=True))
    irrelevant_cos = sum(a * b for a, b in zip(q, _unit(doc_vecs[1]), strict=True))
    if not relevant_cos > irrelevant_cos:
        raise SystemExit(
            f"[fail-loud] OVH sanity check failed: relevant={relevant_cos:.4f} "
            f"irrelevant={irrelevant_cos:.4f} -- the relevant text must out-score the "
            "irrelevant one; wrong model id or query-prefix mismatch?"
        )
    print(f"[sanity] OVH: relevant={relevant_cos:.4f} irrelevant={irrelevant_cos:.4f} -> OK")


def _report_arm(name: str, ndcg: dict[int, float | None]) -> None:
    parts = ", ".join(
        f"nDCG@{k}={v:.4f}" if v is not None else f"nDCG@{k}=n/a" for k, v in sorted(ndcg.items())
    )
    print(f"  {name:>8}  {parts}")


def run(args: argparse.Namespace) -> int:
    """Embed both arms over the fixed real-query corpus, decide, write the artifact."""
    cases = load_probe_set(Path(args.probe))
    notes_by_entity, cases = _build_corpus(cases)
    note_names = list(notes_by_entity)
    note_texts = [notes_by_entity[name] for name in note_names]
    query_texts = [case.query for case in cases]

    base_url, token = _ovh_credentials()
    asyncio.run(_sanity_check_ovh(base_url, token, args.ovh_model))

    local_notes, local_queries = (
        asyncio.run(_embed_local(note_texts, "document")),
        asyncio.run(_embed_local(query_texts, "query")),
    )
    _assert_vectors(local_notes, args.local_dim, "0.6b note")
    _assert_vectors(local_queries, args.local_dim, "0.6b query")

    ovh_notes = asyncio.run(_embed_ovh(note_texts, "document", base_url, token, args.ovh_model))
    ovh_queries = asyncio.run(_embed_ovh(query_texts, "query", base_url, token, args.ovh_model))
    _assert_vectors(ovh_notes, args.ovh_dim, "8b-ovh note")
    _assert_vectors(ovh_queries, args.ovh_dim, "8b-ovh query")

    ndcg_06b = score_arm(cases, note_names, local_notes, local_queries, _KS)
    ndcg_ovh = score_arm(cases, note_names, ovh_notes, ovh_queries, _KS)

    print(
        f"=== FRE-817 corpus A/B -- probe={args.probe} n_notes={len(note_names)} n_queries={len(cases)} ==="
    )
    _report_arm("0.6b", ndcg_06b)
    _report_arm("8b-ovh", ndcg_ovh)

    decision_ndcg_06b = ndcg_06b[_DECISION_K]
    decision_ndcg_ovh = ndcg_ovh[_DECISION_K]
    if decision_ndcg_06b is None or decision_ndcg_ovh is None:
        raise SystemExit(
            f"[fail-loud] nDCG@{_DECISION_K} is undefined for at least one arm "
            "(no non-control cases?) -- cannot decide"
        )
    candidates = [
        EmbedderCandidate(name="0.6b", kind="open_weight", mean_ndcg=decision_ndcg_06b),
        EmbedderCandidate(name="8b-ovh", kind="open_weight", mean_ndcg=decision_ndcg_ovh),
    ]
    decision = decide_embedder(candidates, margin=PRE_REGISTERED_MARGIN_NDCG)
    print(f"decision: winner={decision.winner} margin_cleared={decision.margin_cleared}")
    print(f"reasoning: {decision.reasoning}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"corpus-ab-{args.run_id}.json"
    out_path.write_text(
        json.dumps(
            {
                "probe": args.probe,
                "n_notes": len(note_names),
                "n_queries": len(cases),
                "ndcg": {"0.6b": ndcg_06b, "8b-ovh": ndcg_ovh},
                "decision_metric": f"ndcg@{_DECISION_K}",
                "pre_registered_margin": PRE_REGISTERED_MARGIN_NDCG,
                "decision": {
                    "winner": decision.winner,
                    "winner_kind": decision.winner_kind,
                    "margin_cleared": decision.margin_cleared,
                    "reasoning": decision.reasoning,
                },
            },
            indent=2,
        )
    )
    print(f"written: {out_path}  (gitignored -- commit only the curated research writeup)")
    return 0


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", default=DEFAULT_PROBE, help="Fixed real-query probe YAML.")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output dir (gitignored).")
    parser.add_argument("--run-id", default="run", help="Run identifier for the output filename.")
    parser.add_argument("--local-dim", type=int, default=1024, help="Expected 0.6b vector width.")
    parser.add_argument("--ovh-dim", type=int, default=4096, help="Expected OVH 8B vector width.")
    parser.add_argument(
        "--ovh-model", default="Qwen3-Embedding-8B", help="OVH AI Endpoints model id."
    )
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
