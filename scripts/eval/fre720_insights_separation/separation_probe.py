"""FRE-720 -- separation-probe measurement gate on the insights corpus (ADR-0105 D10 / AC-8).

Reuses the FRE-670/ADR-0103 separation-probe *instrument* -- the pure cosine-separation
statistics in ``scripts/eval/fre435_memory_recall/{separation_report,calibration}.py`` --
on the real ``agent-captains-reflections-*`` proposal corpus, to decide whether the
deployed embedder opens a clean cosine floor between "same idea, reworded" (positive)
and "same category, genuinely distinct idea" (hard negative) proposals.

This is a measurement gate, not a mechanism build: it produces a versioned artifact
(``probe_result.json``, committed) and a branch decision that FRE-721 (T7) mechanically
checks itself against (``probe_result.json["decision"]``) -- see the FRE-720 plan's
Downstream contract. No production (``src/``) code changes.

OFFLINE-ADJACENT BY DESIGN: this script embeds a committed, real-proposal corpus
(``corpus.yaml``) via the deployed 0.6B embedder (``embeddings:8503``/``localhost:8503``)
-- it touches no ES/Neo4j/Postgres substrate at run time (the corpus was pulled from the
real ``agent-captains-reflections-*`` index once, 2026-07-05, and committed so the probe
replays without live ES access). The defensive test-substrate env pinning below mirrors
``separation_benchmark.py``'s convention (stay past the ADR-0099 validator) even though
no substrate is touched.

Run::

    uv run python scripts/eval/fre720_insights_separation/separation_probe.py
"""

from __future__ import annotations

import os

for _key, _value in {
    "APP_ENV": "test",
    "AGENT_NEO4J_URI": "bolt://localhost:7688",
    "AGENT_ELASTICSEARCH_URL": "http://localhost:9201",
    "AGENT_DATABASE_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
}.items():
    os.environ.setdefault(_key, _value)

import asyncio  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Literal  # noqa: E402

from scripts.eval.fre435_memory_recall.calibration import propose_floor  # noqa: E402
from scripts.eval.fre435_memory_recall.separation_report import (  # noqa: E402
    SeparationStats,
    summarize_separation,
)
from scripts.eval.fre720_insights_separation.decision import decide_branch  # noqa: E402
from scripts.eval.fre720_insights_separation.probe_pairs import (  # noqa: E402
    Corpus,
    load_corpus,
    load_pair_set,
)

DEFAULT_CORPUS = "scripts/eval/fre720_insights_separation/corpus.yaml"
DEFAULT_PAIRS = "scripts/eval/fre720_insights_separation/pairs.yaml"
DEFAULT_TELEMETRY_OUT = "telemetry/evaluation/fre720-insights-separation"
DEFAULT_ARTIFACT_OUT = "scripts/eval/fre720_insights_separation/probe_result.json"

_TOTAL_CORPUS_AT_PULL_TIME = 1857  # agent-captains-reflections-* non-eval docs, 2026-07-05
_QUERY_DESCRIPTION = (
    "35 real proposals selected as same-idea clusters (fast-path/reduce-LLM-calls, "
    "capture_write_failed retry/alert, ES retry/backoff, ux clarification-gate, ux "
    "tool-visibility) plus same-category-and-topical-family hard-negative singletons "
    "(verify-before-acting, add-telemetry-for-X, skip-work-for-trivial-input families); "
    "see pairs.yaml notes for per-pair provenance."
)
_CORPUS_SOURCE = (
    "agent-captains-reflections-* (Elasticsearch), field `proposed_change`, "
    "excluding eval_mode: true docs"
)


def _file_sha256(path: Path) -> str:
    """SHA-256 of a file's raw bytes (reproducibility provenance, not commit-history-dependent)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_short_sha_and_dirty(repo_root: Path) -> tuple[str, bool]:
    """The running tree's short commit SHA and whether it has uncommitted changes."""
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    return sha, dirty


def _time_window(corpus: Corpus) -> tuple[str, str]:
    """The min/max `timestamp` across every corpus entry (ISO-8601 strings sort correctly)."""
    timestamps = sorted(e.timestamp for e in corpus.entries.values() if e.timestamp)
    if not timestamps:
        raise SystemExit("[fail-loud] no corpus entry carries a timestamp")
    return timestamps[0], timestamps[-1]


def build_artifact(
    *,
    corpus_size: int,
    total_corpus_at_pull_time: int,
    positive_pairs: int,
    negative_pairs: int,
    stats: SeparationStats,
    floor_recall: float,
    floor_fpr: float,
    floor_value: float,
    floor_youden_j: float,
    decision: Literal["semantic", "fallback"],
    time_window_start: str,
    time_window_end: str,
    probe_code_version: str,
    git_dirty: bool,
    corpus_sha256: str,
    pairs_sha256: str,
    run_id: str,
) -> dict[str, object]:
    """Build the versioned AC-8 probe artifact (pure -- no substrate, no embedder call).

    Args:
        corpus_size: Number of real-proposal docs in the committed corpus.
        total_corpus_at_pull_time: Total non-eval `proposed_change` docs in the live
            index at pull time (context for how small a slice `corpus_size` is).
        positive_pairs: Number of labeled positive (same-idea) pairs.
        negative_pairs: Number of labeled negative (hard-near-miss) pairs.
        stats: The measured positive/negative cosine separation summary.
        floor_recall: The `propose_floor` recall at the chosen floor.
        floor_fpr: The `propose_floor` false-positive-rate at the chosen floor.
        floor_value: The chosen floor's cosine threshold.
        floor_youden_j: The chosen floor's Youden's J.
        decision: The D10 branch decision (`decide_branch(stats)`).
        time_window_start: Earliest corpus-entry timestamp (ISO-8601).
        time_window_end: Latest corpus-entry timestamp (ISO-8601).
        probe_code_version: Git short SHA of the tree that produced this run.
        git_dirty: Whether the tree had uncommitted changes at run time.
        corpus_sha256: SHA-256 of the loaded `corpus.yaml` bytes.
        pairs_sha256: SHA-256 of the loaded `pairs.yaml` bytes.
        run_id: A stable identifier for this run.

    Returns:
        The JSON-serializable artifact, with every ADR-0105 AC-8-required field.
    """
    return {
        "corpus_source": _CORPUS_SOURCE,
        "query_description": _QUERY_DESCRIPTION,
        "time_window": {"start": time_window_start, "end": time_window_end},
        "item_counts": {
            "corpus_docs": corpus_size,
            "total_corpus_at_pull_time": total_corpus_at_pull_time,
        },
        "pair_counts": {"positive": positive_pairs, "negative": negative_pairs},
        "cosine_distributions": stats.__dict__,
        "chosen_floor": {
            "floor": floor_value,
            "recall": floor_recall,
            "fpr": floor_fpr,
            "youden_j": floor_youden_j,
        },
        "decision": decision,
        "probe_code_version": probe_code_version,
        "git_dirty": git_dirty,
        "corpus_sha256": corpus_sha256,
        "pairs_sha256": pairs_sha256,
        "run_id": run_id,
    }


def _assert_vector(entry_id: str, vector: list[float], dimensions: int) -> None:
    """Fail loud on a wrong-length or all-zero (degenerate) embedding."""
    if len(vector) != dimensions:
        raise SystemExit(
            f"[fail-loud] {entry_id} vector length {len(vector)} != expected {dimensions}"
        )
    if not any(x != 0.0 for x in vector):
        raise SystemExit(f"[fail-loud] {entry_id} vector is all-zero (failed embedding)")


async def _embed_corpus(corpus: Corpus) -> dict[str, list[float]]:
    """Embed every corpus text once via the deployed production embedder (document mode)."""
    from personal_agent.config import get_settings  # noqa: PLC0415
    from personal_agent.memory.embeddings import generate_embeddings_batch  # noqa: PLC0415

    entry_ids = list(corpus.entries)
    texts = [corpus[entry_id].text for entry_id in entry_ids]
    vectors = await generate_embeddings_batch(texts, mode="document")
    dimensions = get_settings().embedding_dimensions
    for entry_id, vector in zip(entry_ids, vectors, strict=True):
        _assert_vector(entry_id, vector, dimensions)
    return dict(zip(entry_ids, vectors, strict=True))


async def run(
    corpus_path: Path,
    pairs_path: Path,
    telemetry_out: Path,
    artifact_out: Path,
    repo_root: Path,
) -> int:
    """Load the real corpus + pairs, embed, measure separation, write the artifact."""
    from personal_agent.memory.embeddings import cosine_similarity  # noqa: PLC0415

    corpus = load_corpus(corpus_path)
    pairs = load_pair_set(pairs_path, corpus)

    vectors = await _embed_corpus(corpus)

    positives: list[float] = []
    negatives: list[float] = []
    for pair in pairs:
        score = cosine_similarity(vectors[pair.a], vectors[pair.b])
        (positives if pair.label == "positive" else negatives).append(score)

    stats = summarize_separation(positives, negatives)
    floor = propose_floor(positives, negatives)
    decision = decide_branch(stats)
    time_start, time_end = _time_window(corpus)
    probe_code_version, git_dirty = _git_short_sha_and_dirty(repo_root)
    corpus_sha256 = _file_sha256(corpus_path)
    pairs_sha256 = _file_sha256(pairs_path)
    run_id = f"run-{probe_code_version}-{corpus_sha256[:8]}"

    artifact = build_artifact(
        corpus_size=len(corpus.entries),
        total_corpus_at_pull_time=_TOTAL_CORPUS_AT_PULL_TIME,
        positive_pairs=sum(1 for p in pairs if p.label == "positive"),
        negative_pairs=sum(1 for p in pairs if p.label == "negative"),
        stats=stats,
        floor_recall=floor.recall,
        floor_fpr=floor.false_positive_rate,
        floor_value=floor.floor,
        floor_youden_j=floor.youden_j,
        decision=decision,
        time_window_start=time_start,
        time_window_end=time_end,
        probe_code_version=probe_code_version,
        git_dirty=git_dirty,
        corpus_sha256=corpus_sha256,
        pairs_sha256=pairs_sha256,
        run_id=run_id,
    )

    print("=== FRE-720 separation probe -- ADR-0105 D10 / AC-8 ===")
    print(json.dumps(artifact, indent=2))
    verdict = "CLEAN floor" if stats.clean_floor else "OVERLAP"
    print(
        f"pos[min/med/p5={stats.pos_min:.3f}/{stats.pos_median:.3f}/{stats.pos_p5:.3f}]  "
        f"neg[max/med/p95={stats.neg_max:.3f}/{stats.neg_median:.3f}/{stats.neg_p95:.3f}]  "
        f"overlap[neg>=minpos={stats.neg_above_min_pos},pos<=maxneg={stats.pos_below_max_neg}]  "
        f"-> {verdict} -> decision={decision}"
    )

    telemetry_out.mkdir(parents=True, exist_ok=True)
    (telemetry_out / f"separation-report-{run_id}.json").write_text(json.dumps(artifact, indent=2))
    artifact_out.write_text(json.dumps(artifact, indent=2) + "\n")
    print(f"written: {artifact_out} (committed) and {telemetry_out} (gitignored, full detail)")
    return 0


def main() -> int:
    """CLI entry point."""
    repo_root = Path(__file__).resolve().parents[3]
    return asyncio.run(
        run(
            corpus_path=repo_root / DEFAULT_CORPUS,
            pairs_path=repo_root / DEFAULT_PAIRS,
            telemetry_out=repo_root / DEFAULT_TELEMETRY_OUT,
            artifact_out=repo_root / DEFAULT_ARTIFACT_OUT,
            repo_root=repo_root,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
