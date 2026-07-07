"""FRE-821 -- ADR-0112 AC-6 managed <-> local-fallback embedder failover probe.

Live-only tool for MASTER to run at actual deploy time, once the same-model
local-8B fallback is provisioned and reachable (build cannot run this: there is
no local fallback endpoint to compare against until that deploy step happens).
Never run in CI; credentials come from `pass`, never persisted.

Two subcommands answer AC-6's two halves:

* ``cosine``    -- embed a fixed >=50-input set through both the managed and
  local-fallback endpoints; the SAME model on both sides should yield
  near-identical vectors. Fails loud if the minimum pairwise cosine across all
  inputs drops below 0.999.
* ``retrieval-overlap`` -- for a fixed query set, embed each query through both
  endpoints and rank the *existing* Neo4j entity_embedding index (no re-embed);
  fails loud if the mean top-k(10) name overlap drops below 0.95.

Usage (master, live credentials via `pass`):

    uv run python -m scripts.eval.fre821_embedder_failover_probe.probe cosine
    uv run python -m scripts.eval.fre821_embedder_failover_probe.probe retrieval-overlap
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from scripts.eval.fre435_memory_recall.probes import load_probe_set

_PROBE_YAML = "scripts/eval/fre435_memory_recall/semantic_probe.yaml"
_MIN_COSINE = 0.999
_MIN_OVERLAP = 0.95
_TOP_K = 10


def _unit(vec: Sequence[float]) -> list[float]:
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / norm for x in vec]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    ua, ub = _unit(a), _unit(b)
    return sum(x * y for x, y in zip(ua, ub, strict=True))


def _read_pass(entry: str) -> str:
    """Read one secret from `pass` (never logged/persisted)."""
    out = subprocess.run(["pass", "show", entry], capture_output=True, text=True, check=True)
    value = out.stdout.splitlines()[0].strip() if out.stdout else ""
    if not value:
        raise SystemExit(f"[fail-loud] `pass show {entry}` returned empty")
    return value


def _managed_credentials() -> tuple[str, str, str]:
    """Managed (OVH) endpoint base URL, token, model -- from `pass`, never the shell env."""
    return (
        _read_pass("seshat/AGENT_MANAGED_EMBEDDING_ENDPOINT"),
        _read_pass("seshat/AGENT_MANAGED_EMBEDDING_TOKEN"),
        _read_pass("seshat/AGENT_MANAGED_EMBEDDING_MODEL"),
    )


async def _embed_both(
    texts: Sequence[str],
    mode: str,
    fallback_endpoint: str,
    fallback_model: str,
) -> tuple[list[list[float]], list[list[float]]]:
    """Embed *texts* through both the managed and local-fallback endpoints."""
    from personal_agent.memory.embeddings import _call_embeddings_api, _embed_managed

    base_url, token, model = _managed_credentials()
    managed_task = _embed_managed(list(texts), base_url, token, model)

    prefix = (
        "Instruct: Given a query, retrieve relevant entities and passages\nQuery: "
        if mode == "query"
        else ""
    )
    fallback_texts = [f"{prefix}{t}" for t in texts]
    fallback_response = await _call_embeddings_api(
        texts=fallback_texts, model=fallback_model, endpoint=fallback_endpoint
    )
    managed_vecs = await managed_task
    fallback_vecs = [[float(x) for x in d.embedding] for d in fallback_response.data]
    return managed_vecs, fallback_vecs


def run_cosine(args: argparse.Namespace) -> int:
    """AC-6 static-vs-dynamic bridge: pairwise cosine over a fixed >=50-input set."""
    cases = load_probe_set(Path(args.probe))
    notes = {
        entity.name.strip().lower(): f"{entity.name}: {entity.description or ''}".strip()
        for case in cases
        for entity in case.seed_entities
    }
    texts = list(notes.values())[: args.n] if args.n else list(notes.values())
    if len(texts) < 50:
        raise SystemExit(
            f"[fail-loud] only {len(texts)} probe texts available, need >= 50 for AC-6"
        )

    managed_vecs, fallback_vecs = asyncio.run(
        _embed_both(texts, "document", args.fallback_endpoint, args.fallback_model)
    )
    cosines = [_cosine(m, f) for m, f in zip(managed_vecs, fallback_vecs, strict=True)]
    mean_cos = sum(cosines) / len(cosines)
    min_cos = min(cosines)
    print(f"=== FRE-821 cosine probe -- n={len(texts)} mean={mean_cos:.6f} min={min_cos:.6f} ===")
    if min_cos < _MIN_COSINE:
        print(f"[FAIL] min pairwise cosine {min_cos:.6f} < {_MIN_COSINE} -- AC-6 NOT satisfied")
        return 1
    print(f"[PASS] min pairwise cosine {min_cos:.6f} >= {_MIN_COSINE}")
    return 0


def run_retrieval_overlap(args: argparse.Namespace) -> int:
    """AC-6 dynamic half: top-k(10) overlap over the EXISTING index, no re-embed."""
    from personal_agent.memory.service import MemoryService

    cases = load_probe_set(Path(args.probe))
    queries = [case.query for case in cases][: args.n] if args.n else [case.query for case in cases]

    async def _run() -> float:
        service = MemoryService()  # fre-375-allow: AC-6 probes the EXISTING (prod) index, read-only
        if not await service.connect():
            raise SystemExit("[fail-loud] could not connect to the existing Neo4j index")
        try:
            managed_vecs, fallback_vecs = await _embed_both(
                queries, "query", args.fallback_endpoint, args.fallback_model
            )
            overlaps = []
            for managed_vec, fallback_vec in zip(managed_vecs, fallback_vecs, strict=True):
                async with service.driver.session() as session:  # type: ignore[union-attr]
                    managed_rows = await service._query_entity_vector_candidates(  # noqa: SLF001
                        session, managed_vec, _TOP_K
                    )
                    fallback_rows = await service._query_entity_vector_candidates(  # noqa: SLF001
                        session, fallback_vec, _TOP_K
                    )
                managed_names = {row["name"] for row in managed_rows}
                fallback_names = {row["name"] for row in fallback_rows}
                if not managed_names and not fallback_names:
                    continue
                overlaps.append(len(managed_names & fallback_names) / max(len(managed_names), 1))
            return sum(overlaps) / len(overlaps) if overlaps else 0.0
        finally:
            await service.disconnect()

    mean_overlap = asyncio.run(_run())
    print(f"=== FRE-821 retrieval-overlap probe -- n_queries={len(queries)} ===")
    print(f"mean_overlap={mean_overlap:.4f}")
    if mean_overlap < _MIN_OVERLAP:
        print(f"[FAIL] mean top-{_TOP_K} overlap < {_MIN_OVERLAP} -- AC-6 NOT satisfied")
        return 1
    print(f"[PASS] mean top-{_TOP_K} overlap {mean_overlap:.4f} >= {_MIN_OVERLAP}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", default=_PROBE_YAML, help="Fixed probe YAML.")
    parser.add_argument("--n", type=int, default=0, help="Cap the number of inputs (0 = all).")
    parser.add_argument(
        "--fallback-endpoint",
        required=True,
        help="Same-model local-fallback embedder endpoint (e.g. http://local-8b:8503/v1).",
    )
    parser.add_argument(
        "--fallback-model",
        default="Qwen/Qwen3-Embedding-8B",
        help="Model id to request from the local-fallback endpoint.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("cosine")
    subparsers.add_parser("retrieval-overlap")

    args = parser.parse_args(argv)
    if args.command == "cosine":
        return run_cosine(args)
    return run_retrieval_overlap(args)


if __name__ == "__main__":
    sys.exit(main())
