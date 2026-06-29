r"""BM25 keyword baseline — the standing lexical-leakage guard (FRE-656 + FRE-670).

A plain keyword search over the stored entity text, run alongside the vector
embedder arms so every probe reports keyword recall next to vector recall. It is
the instrument check behind the FRE-656 finding and the FRE-670 acceptance bar:

* **FRE-656** — on the FRE-489 probe (``bespoke_probe.yaml``) BM25 matches or beats
  the vector embedder, because the queries, while never naming their target entity,
  still share ordinary content vocabulary with the entity *description*. That probe
  tests lexical recall with the label hidden, not semantic recall.
* **FRE-670** — on the vocabulary-divergent probe (``semantic_probe.yaml``) BM25
  must land **materially below** the vector arms on the positives; if it wins or
  ties, the split is still lexical and the ticket is not done.

Dependency-light: a small BM25 over the probe YAML, no substrate, no embedder. The
pure ranking/scoring helpers are unit-tested in
``tests/evaluation/test_fre670_retrieval_baseline.py``.

Run::

    uv run python scripts/eval/fre435_memory_recall/keyword_baseline.py \\
        --probe scripts/eval/fre435_memory_recall/semantic_probe.yaml
"""

from __future__ import annotations

import argparse
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PROBE = Path(__file__).parent / "bespoke_probe.yaml"
DEFAULT_K_VALUES = (1, 5)

# Query-side stopwords: ordinary function words + the conversational scaffolding
# the probe queries use ("pick up where we left off", "what was the core idea").
# Dropping them keeps BM25 honest — it must match on *content*, not filler.
_STOP = set(
    "the a an of to in on and or is are was were be been being it its this that "
    "what left off pick up we core idea i m my you your for with how do does did "
    "lets let s about can could would there here some more most".split()
)


def _content_tokens(text: str | None) -> list[str]:
    """Lowercase content tokens (>2 chars, non-stopword) of ``text``."""
    return [
        t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _STOP and len(t) > 2
    ]


def _bm25_scores(
    query: Sequence[str], docs: Sequence[Sequence[str]], k1: float = 1.5, b: float = 0.75
) -> list[float]:
    """Score ``query`` against each document in ``docs`` with Okapi BM25.

    Args:
        query: Tokenised query.
        docs: Tokenised candidate documents (the co-resident entity texts).
        k1: BM25 term-frequency saturation.
        b: BM25 length-normalisation.

    Returns:
        One BM25 score per document, in ``docs`` order.
    """
    n = len(docs)
    if n == 0:
        return []
    df: Counter[str] = Counter()
    for doc in docs:
        df.update(set(doc))
    idf = {w: math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5)) for w in df}
    avgdl = sum(len(d) for d in docs) / n or 1.0

    scores: list[float] = []
    for doc in docs:
        tf = Counter(doc)
        score = 0.0
        for w in query:
            if w in tf:
                score += (
                    idf.get(w, 0.0)
                    * tf[w]
                    * (k1 + 1)
                    / (tf[w] + k1 * (1 - b + b * len(doc) / avgdl))
                )
        scores.append(score)
    return scores


def bm25_rank(
    query_tokens: Sequence[str], docs: Sequence[Sequence[str]], names: Sequence[str]
) -> list[str]:
    """Rank ``names`` for a query by BM25, dropping zero-score docs.

    A document that shares no content token with the query scores 0 and is excluded
    entirely — a vocabulary-divergent query that matches nothing must NOT score a
    phantom hit on insertion order (codex review #4: zero-score / tie-order traps).
    Ties break deterministically by name, so the ranking is independent of corpus
    insertion order.

    Args:
        query_tokens: Tokenised query.
        docs: Tokenised candidate documents, aligned with ``names``.
        names: Candidate entity names, aligned with ``docs``.

    Returns:
        Names with a strictly-positive BM25 score, best first; ties by name.
    """
    scores = _bm25_scores(query_tokens, docs)
    scored = [(name, score) for name, score in zip(names, scores, strict=True) if score > 0.0]
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return [name for name, _ in scored]


def fractional_recall_at_k(ranked: Sequence[str], expected: set[str], k: int) -> float:
    """Fraction of the expected set recalled within the top ``k`` (harness metric).

    Matches ``metrics.recall_at_k`` semantics (codex review #4: binary-vs-fractional
    mismatch) so the BM25 column is comparable to the vector arm. Returns 0.0 when
    nothing is expected (controls are scored separately, not here).

    Args:
        ranked: Ranked candidate names.
        expected: Expected names (already normalised by the caller).
        k: Cut-off.

    Returns:
        ``|top-k ∩ expected| / |expected|`` (0.0 if ``expected`` is empty).
    """
    if not expected:
        return 0.0
    return len(set(ranked[:k]) & expected) / len(expected)


@dataclass(frozen=True)
class ArmResult:
    """Aggregate BM25 recall for one document mode (name-only or name+description).

    Attributes:
        label: Human label for the document mode.
        recall_overall: Mean recall@k over all positives.
        recall_by_register: Mean recall@k split by ``register:`` tag.
        positives_scored: Number of positives scored.
        controls_total: Number of controls in the probe.
        control_no_match: Controls for which keyword search returns nothing (clean
            keyword abstention — no lexical neighbour anywhere in the corpus).
        name_leakage: Positives whose query contains an expected entity name (must
            be 0 — the referential discipline; a non-zero value invalidates the run).
    """

    label: str
    recall_overall: dict[int, float]
    recall_by_register: dict[str, dict[int, float]]
    positives_scored: int
    controls_total: int = 0
    control_no_match: int = 0
    name_leakage: int = 0


def _register(case: Mapping[str, Any]) -> str:
    """Return the case's register (``natural`` / ``imagery`` / ``unknown``)."""
    for tag in case.get("tags", []):
        if tag.startswith("register:"):
            return tag[len("register:") :]
    return "unknown"


def _is_positive(case: Mapping[str, Any]) -> bool:
    tags = case.get("tags", [])
    if "type:positive" in tags or "type:control" in tags:
        return "type:positive" in tags
    # Back-compat with bespoke_probe.yaml (no type: tags): positive == has expected.
    return bool(case.get("expected", {}).get("entity_names"))


def _is_control(case: Mapping[str, Any]) -> bool:
    tags = case.get("tags", [])
    if "type:control" in tags:
        return True
    return "control" in tags and not case.get("expected", {}).get("entity_names")


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def evaluate_probe(
    cases: Sequence[Mapping[str, Any]],
    use_description: bool,
    k_values: Sequence[int] = DEFAULT_K_VALUES,
) -> ArmResult:
    """Score BM25 keyword recall over a probe's co-resident entity corpus.

    Args:
        cases: Raw probe cases (YAML mappings).
        use_description: Rank over ``name + description`` (realistic) vs name only.
        k_values: Recall cut-offs to report.

    Returns:
        The aggregate :class:`ArmResult`.
    """
    docs: list[list[str]] = []
    names: list[str] = []
    for case in cases:
        for entity in case.get("seed_entities", []) or []:
            text = entity["name"]
            if use_description:
                text = f"{text} {entity.get('description') or ''}"
            docs.append(_content_tokens(text))
            names.append(entity["name"].strip().lower())

    by_register: dict[str, dict[int, list[float]]] = {}
    overall: dict[int, list[float]] = {k: [] for k in k_values}
    positives = controls = control_no_match = leakage = 0

    for case in cases:
        if _is_control(case):
            controls += 1
            ranked = bm25_rank(_content_tokens(case.get("query")), docs, names)
            if not ranked:
                control_no_match += 1
            continue
        if not _is_positive(case):
            continue
        expected = {n.strip().lower() for n in case["expected"]["entity_names"] if n.strip()}
        if not expected:
            continue
        positives += 1
        query_low = case.get("query", "").lower()
        if any(name in query_low for name in expected):
            leakage += 1
        ranked = bm25_rank(_content_tokens(case.get("query")), docs, names)
        register = _register(case)
        reg_bucket = by_register.setdefault(register, {k: [] for k in k_values})
        for k in k_values:
            recall = fractional_recall_at_k(ranked, expected, k)
            overall[k].append(recall)
            reg_bucket[k].append(recall)

    return ArmResult(
        label="name + description" if use_description else "name ONLY",
        recall_overall={k: _mean(v) for k, v in overall.items()},
        recall_by_register={
            reg: {k: _mean(v) for k, v in buckets.items()} for reg, buckets in by_register.items()
        },
        positives_scored=positives,
        controls_total=controls,
        control_no_match=control_no_match,
        name_leakage=leakage,
    )


def _print_arm(result: ArmResult, k_values: Sequence[int]) -> None:
    """Print one arm's recall table (overall + per-register + controls)."""
    ks = " ".join(f"@{k}={result.recall_overall[k]:.3f}" for k in k_values)
    print(
        f"\nBM25 query <-> {result.label:18s}: recall {ks}  (positives={result.positives_scored})"
    )
    if result.name_leakage:
        print(f"  ⚠️ name-in-query leakage={result.name_leakage} (referential discipline broken)")
    for register in sorted(result.recall_by_register):
        reg = result.recall_by_register[register]
        rk = " ".join(f"@{k}={reg[k]:.3f}" for k in k_values)
        print(f"  register:{register:8s} recall {rk}")
    if result.controls_total:
        print(
            f"  controls: keyword-abstains (no lexical match) "
            f"{result.control_no_match}/{result.controls_total}"
        )


def main() -> None:
    """Print BM25 recall (name-only and name+description) for a probe."""
    parser = argparse.ArgumentParser(description="BM25 keyword baseline for a recall probe")
    parser.add_argument("--probe", default=str(DEFAULT_PROBE), help="Probe YAML path.")
    parser.add_argument(
        "--k", type=int, nargs="+", default=list(DEFAULT_K_VALUES), help="recall@k cut-offs."
    )
    args = parser.parse_args()
    cases = yaml.safe_load(Path(args.probe).read_text())["cases"]
    k_values = tuple(sorted(set(args.k)))

    print(f"=== BM25 keyword baseline — {args.probe} ({len(cases)} cases) ===")
    for use_description in (False, True):
        _print_arm(evaluate_probe(cases, use_description, k_values), k_values)
    print(
        "\nAC2 (FRE-670): the name+description recall@5 above must land MATERIALLY BELOW the "
        "vector arm's recall@5 (run_embedder_benchmark.sh) on the positives."
    )


if __name__ == "__main__":
    main()
